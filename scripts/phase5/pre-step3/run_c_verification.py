"""Phase 5 pre-Step3 批次 3：只跑 C 策略的 3 次真实验证。

前置：``start_prestep3_services.py`` 已拉起隔离 Agent(8001) + Spring(8081)。

流程（每一步失败即停止，不重试超过 1 次）：

1. ``setup_snapshot.py --apply --index-knowledge``（若快照不存在）；
2. ``freeze_check --write-baseline`` + ``qdrant_freeze_check --write-baseline``；
3. 每次：restore（含决策 1/2 的原子性检查）→ MySQL/Qdrant 冻结校验 →
   同一 conversation、同一 NEUTRAL_QUESTION 的 SSE 调用 → 记录
   run_id / request_id / finish_reason / usage / latency / Manifest /
   Citation / Context Hash；
4. 每次结束再次 restore，并再次校验冻结一致性；
5. 3 次后 ``cleanup.py`` 清理隔离资源。

本脚本不打印、不写盘任何密钥或完整 prompt；只写 hash、id、状态与计数。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from snapshot_common import (
    FIXTURE_MARKER,
    NEUTRAL_QUESTION,
    PASSWORD,
    assert_isolated_state,
    connect_mysql,
    write_json,
)

HERE = Path(__file__).resolve().parent
PILOT_DIR = HERE.parent / "pilot"
REQUIRED_LLM_MAX_TOKENS = 4096
HISTORY_MARKER = FIXTURE_MARKER


def run_script(name: str | Path, *args: str, timeout: int = 600) -> dict[str, Any]:
    candidate = Path(name)
    script = candidate if candidate.is_absolute() else HERE / candidate
    command = [sys.executable, str(script), *args]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"\n$ {' '.join(command[1:])}")
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    print(f"[exit={completed.returncode} elapsedMs={elapsed_ms}]")
    return {
        "script": name,
        "args": list(args),
        "exitCode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "elapsedMs": elapsed_ms,
    }


def must(result: dict[str, Any], label: str) -> None:
    if result["exitCode"] != 0:
        raise SystemExit(f"{label} 失败（exit={result['exitCode']}），停止执行")


def api(
    base: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    timeout: float = 30,
    **kwargs: Any,
) -> Any:
    headers = dict(kwargs.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(method, base + path, headers=headers, timeout=timeout, **kwargs)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:500]}
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} HTTP {response.status_code}: {str(data)[:400]}")
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"{method} {path} code={data.get('code')}: {str(data)[:400]}")
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def login(spring: str, manifest: dict[str, Any]) -> str:
    username = manifest["dataset"]["username"]
    data = api(
        spring,
        "POST",
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    return str(data["token"])


def chat(spring: str, token: str, conversation_id: int, question: str, label: str) -> dict[str, Any]:
    """发送同一问题并解析 Spring SSE。"""
    client_request_id = str(uuid.uuid4())
    started = time.monotonic()
    events: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}
    deltas: list[str] = []
    current_event: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal current_event, data_lines
        if current_event is None:
            data_lines = []
            return
        raw = "\n".join(data_lines)
        payload: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"raw": raw[:500]}
        events.append(current_event)
        payloads[current_event] = payload
        if current_event == "message.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        current_event = None
        data_lines = []

    url = f"{spring}/api/conversations/{conversation_id}/messages"
    with requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json={"content": question, "clientRequestId": client_request_id},
        stream=True,
        timeout=300,
    ) as response:
        response.encoding = "utf-8"
        http_status = response.status_code
        for raw in response.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.rstrip("\r")
            if line == "":
                flush()
            elif line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    answer = "".join(deltas)
    return {
        "label": label,
        "conversationId": conversation_id,
        "clientRequestId": client_request_id,
        "httpStatus": http_status,
        "events": events,
        "start": payloads.get("message.start"),
        "end": payloads.get("message.end"),
        "error": payloads.get("message.error"),
        "answer": answer,
        "answerChars": len(answer),
        "empty": not answer.strip(),
        "elapsedMs": elapsed_ms,
    }


def agent_health(agent: str) -> dict[str, Any]:
    return api(agent, "GET", "/health")


def agent_diagnostics(agent: str) -> dict[str, Any]:
    return api(agent, "GET", "/phase5/llm-diagnostics")


def agent_context_hashes(agent: str) -> dict[str, Any]:
    return api(agent, "GET", "/phase5/prestep3-context-hashes")


def rabbit_consumers(stamp: str) -> dict[str, int]:
    response = requests.get(
        "http://127.0.0.1:15672/api/queues",
        auth=("guest", "guest"),
        timeout=10,
    )
    response.raise_for_status()
    result: dict[str, int] = {}
    for queue in response.json():
        name = str(queue.get("name") or "")
        if stamp in name:
            result[name] = int(queue.get("consumers") or 0)
    return result


def row_query(database: str, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    connection = connect_mysql(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()
    finally:
        connection.close()


def memory_collection_state(state: dict[str, Any]) -> dict[str, Any]:
    """只读核对默认 Memory collection；不存在记为 ABSENT。"""
    import qdrant_freeze_check as qdrant

    base, headers = qdrant.qdrant_settings()
    name = state["defaultMemoryCollection"]
    response = requests.get(f"{base}/collections/{name}", headers=headers, timeout=30)
    if response.status_code == 404:
        return {"collection": name, "exists": False, "pointCount": 0, "combined": None}
    if response.status_code >= 400:
        raise RuntimeError(f"读取默认 Memory collection 失败：HTTP {response.status_code}")
    points, fetched = qdrant.scroll_all(base, headers, name)
    return {
        "collection": name,
        "exists": True,
        "pointCount": fetched,
        "combined": qdrant.combined_hash(points),
    }


def parse_manifest(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def validate_manifest(manifest: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if not manifest:
        return {
            "problems": ["Manifest 缺失或无法解析"],
            "revisionEntry": False,
            "evidenceEntry": False,
            "intent": None,
        }
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return {
            "problems": ["Manifest.sources 不是列表"],
            "revisionEntry": False,
            "evidenceEntry": False,
            "intent": manifest.get("intent"),
        }

    intent = manifest.get("intent")
    revision_id = int(snapshot["dataset"]["revisionId"])
    evidence_id = int(snapshot["dataset"]["evidenceId"])
    # 普通知识问答（非复盘）按策略 revision=SKIP，trajectory 区块不会出现；
    # 只有 intent=LEARNING_REVIEW 时才要求 revision 对应 entry。Evidence 在
    # 问题含「依据」时为 REQUIRED，任何 intent 下都要求指向快照 evidenceId。
    revision_entries = [
        source
        for source in sources
        if str(source.get("source_id", "")).startswith("revision:")
    ]
    evidence_entries = [
        source
        for source in sources
        if str(source.get("source_id", "")) == f"evidence:{evidence_id}"
    ]
    if not evidence_entries:
        problems.append(f"Manifest 缺少 evidence:{evidence_id} entry")
    if intent == "LEARNING_REVIEW" and not revision_entries:
        problems.append("复盘 intent 下缺少 revision entry")
    if revision_entries and not any(
        str(item.get("source_id")) == f"revision:{revision_id}" for item in revision_entries
    ):
        problems.append(f"revision entry 未指向快照 revisionId={revision_id}")

    required_fields = (
        "type",
        "source_id",
        "retrieval_status",
        "injection_status",
        "reason",
        "estimated_tokens",
    )
    for source in sources:
        missing = [field for field in required_fields if field not in source]
        if missing:
            problems.append(f"source {source.get('source_id')} 缺少字段 {missing}")

    # token 拆分守恒：Manifest 的 `budget.per_type[type].used` 是选中 entry
    # 的 estimated_tokens 之和；两者不等说明 section 估算与拆分不一致。
    # 多 source 的同一 section（trajectory / evidence_status）用 divmod 均分，
    # 各项最多相差 1，单独校验。
    groups: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        kind = str(source.get("type") or "")
        base = kind[: -len("_source")] if kind.endswith("_source") else kind
        groups.setdefault(base, []).append(source)
    per_type = ((manifest.get("budget") or {}).get("per_type") or {})
    for base, items in groups.items():
        selected = [
            int(item.get("estimated_tokens") or 0)
            for item in items
            if item.get("injection_status") in {"SELECTED", "TRUNCATED"}
        ]
        declared = (per_type.get(base) or {}).get("used")
        if declared is not None and int(declared) != sum(selected):
            problems.append(
                f"{base} token 拆分不守恒：entries={sum(selected)} per_type={declared}"
            )
    for base in ("trajectory", "evidence_status"):
        items = groups.get(base) or []
        values = [int(item.get("estimated_tokens") or 0) for item in items]
        if len(values) > 1 and max(values) - min(values) > 1:
            problems.append(f"{base} section token 拆分不均衡：{values}")
        if items and len({item.get("injection_status") for item in items}) != 1:
            problems.append(f"同一 {base} section 的 injection_status 不一致")

    return {
        "problems": problems,
        "intent": intent,
        "revisionEntry": bool(revision_entries),
        "evidenceEntry": bool(evidence_entries),
        "trajectoryTokens": [
            int(item.get("estimated_tokens") or 0) for item in (groups.get("trajectory") or [])
        ],
        "sourceCount": len(sources),
    }


def validate_citations(
    citations: Any, database: str, space_id: int
) -> dict[str, Any]:
    problems: list[str] = []
    items = citations if isinstance(citations, list) else []
    if not items:
        return {"citations": [], "problems": ["Citation 为空"], "traceable": False}
    traceable = 0
    for item in items:
        chunk_id = item.get("chunkId") if isinstance(item, dict) else None
        document_id = item.get("documentId") if isinstance(item, dict) else None
        if chunk_id is None:
            problems.append(f"citation 缺少 chunkId：{item}")
            continue
        row = row_query(
            database,
            "SELECT dc.id, dc.document_id, d.learning_space_id, d.status "
            "FROM document_chunk dc JOIN document d ON d.id = dc.document_id "
            "WHERE dc.id=%s",
            (int(chunk_id),),
        )
        if not row:
            problems.append(f"citation chunkId={chunk_id} 在数据库中不存在")
            continue
        if int(row["learning_space_id"]) != space_id:
            problems.append(f"citation chunkId={chunk_id} 不属于当前 space")
            continue
        if document_id is not None and int(document_id) != int(row["document_id"]):
            problems.append(f"citation documentId 与 chunk 不一致：{chunk_id}")
            continue
        traceable += 1
    return {
        "citations": items,
        "problems": problems,
        "traceable": traceable == len(items) and bool(items),
    }


def preflight(state: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    assert_isolated_state(state)
    spring = f"http://127.0.0.1:{state['springPort']}"
    agent = f"http://127.0.0.1:{state['agentPort']}"
    health = agent_health(agent)
    diagnostics = agent_diagnostics(agent)
    context = agent_context_hashes(agent)
    consumers = rabbit_consumers(state["stamp"])
    problems: list[str] = []
    if health.get("status") != "ok":
        problems.append("Agent /health 非 ok")
    if context.get("contextStrategy") != "C":
        problems.append(f"contextStrategy={context.get('contextStrategy')}，期望 C")
    if int(diagnostics.get("maxTokens") or 0) < REQUIRED_LLM_MAX_TOKENS:
        problems.append(
            f"LLM max_tokens={diagnostics.get('maxTokens')} < {REQUIRED_LLM_MAX_TOKENS}"
        )
    # Spring 自己固定监听 `.result` 队列（结果落库），这是运行链路的正常部分；
    # 真正会写 Understanding/Evidence/Memory 的是 `app.worker`，它必须消费
    # `.request` / `.retry`。这两类队列有消费者才说明分析 worker 没关。
    worker_consumers = {
        name: count
        for name, count in consumers.items()
        if name.endswith(".request") or name.endswith(".retry")
    }
    if any(count != 0 for count in worker_consumers.values()):
        problems.append(f"分析 worker 消费端未关闭：{worker_consumers}")
    _ = spring, snapshot
    return {
        "agentHealth": health,
        "contextStrategy": context.get("contextStrategy"),
        "llmMaxTokens": diagnostics.get("maxTokens"),
        "queueConsumers": consumers,
        "workerConsumers": worker_consumers,
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="start_prestep3_services.py 输出的 STATE_FILE")
    parser.add_argument("--snapshot", help="快照清单路径（默认 <tempDir>/prestep3-snapshot.json）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--results", help="结果 JSON 输出路径")
    parser.add_argument("--skip-setup", action="store_true", help="快照已存在时跳过 setup")
    parser.add_argument("--cleanup", action="store_true", help="3 次成功后执行 cleanup.py")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        result = self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["problems"] else 1
    if not args.state:
        parser.error("非 --self-test 模式需要 --state")

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    assert_isolated_state(state)
    snapshot_path = (
        Path(args.snapshot)
        if args.snapshot
        else Path(state["tempDir"]) / "prestep3-snapshot.json"
    )
    spring = f"http://127.0.0.1:{state['springPort']}"
    agent = f"http://127.0.0.1:{state['agentPort']}"

    results: dict[str, Any] = {
        "kind": "phase5-pre-step3-c-verification",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "strategy": "C",
        "state": {
            "stamp": state["stamp"],
            "db": state["db"],
            "collection": state["collection"],
            "memoryCollection": state["memoryCollection"],
        },
        "steps": [],
        "runs": [],
        "preflight": None,
        "defaultMemory": {},
        "passed": False,
        "problems": [],
    }

    snapshot: dict[str, Any] | None = None
    setup_result = None
    if not args.skip_setup or not snapshot_path.exists():
        setup_result = run_script(
            "setup_snapshot.py",
            args.state,
            "--apply",
            "--index-knowledge",
            "--json-out",
            str(snapshot_path),
            "--force",
            timeout=900,
        )
        results["steps"].append(setup_result)
        must(setup_result, "setup_snapshot --apply --index-knowledge")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert_isolated_state(snapshot["isolation"])

    for script, extra in (
        ("freeze_check.py", ("--write-baseline",)),
        ("qdrant_freeze_check.py", ("--write-baseline",)),
    ):
        result = run_script(script, str(snapshot_path), *extra, timeout=120)
        results["steps"].append(result)
        must(result, f"{script} {' '.join(extra)}")

    preflight_result = preflight(state, snapshot)
    results["preflight"] = preflight_result
    if preflight_result["problems"]:
        raise SystemExit("前置条件失败：" + "；".join(preflight_result["problems"]))

    memory_before = memory_collection_state(state)
    results["defaultMemory"]["before"] = memory_before
    results["embeddingAfterSetup"] = agent_context_hashes(agent)

    token = login(spring, snapshot)
    conversation_id = int(snapshot["dataset"]["conversationId"])
    space_id = int(snapshot["dataset"]["spaceId"])

    failure = None
    for index in range(1, args.runs + 1):
        label = f"run{index}"
        print(f"\n===== C 策略第 {index}/{args.runs} 次 =====")

        # 每组前 restore；脚本内部已含删除前/后与 commit 后的 freeze_check。
        restore_before = run_script(
            "restore_snapshot.py",
            str(snapshot_path),
            "--apply",
            "--prune-run-messages",
            "--json-out",
            str(Path(state["tempDir"]) / f"{label}-restore-before.json"),
            timeout=180,
        )
        results["steps"].append(restore_before)
        if restore_before["exitCode"] != 0:
            failure = f"{label} 前置 restore 失败"
            break

        freeze_before = run_script("freeze_check.py", str(snapshot_path), "--check", timeout=120)
        qdrant_before = run_script(
            "qdrant_freeze_check.py", str(snapshot_path), "--check", timeout=120
        )
        results["steps"].extend([freeze_before, qdrant_before])
        if freeze_before["exitCode"] != 0 or qdrant_before["exitCode"] != 0:
            failure = f"{label} 运行前冻结校验失败"
            break

        context_before = agent_context_hashes(agent)
        diagnostics_before = agent_diagnostics(agent)
        _ = diagnostics_before  # 仅确认路由可用；实际记录取调用后的 last-call
        call = chat(spring, token, conversation_id, NEUTRAL_QUESTION, label)
        time.sleep(0.5)
        diagnostics = agent_diagnostics(agent)
        context_after = agent_context_hashes(agent)
        new_records = context_after.get("records", [])[int(context_before.get("count", 0)) :]
        context_record = new_records[-1] if new_records else None
        embedding_calls = int(context_after.get("embeddingCalls", 0)) - int(
            context_before.get("embeddingCalls", 0)
        )
        embedding_texts = int(context_after.get("embeddingTexts", 0)) - int(
            context_before.get("embeddingTexts", 0)
        )

        run_id = None
        if call.get("end"):
            run_id = call["end"].get("runId")
        if run_id is None and call.get("start"):
            run_id = call["start"].get("runId")
        run_row = None
        if run_id is not None:
            run_row = row_query(
                state["db"],
                "SELECT id,status,trace_id,context_manifest,duration_ms,error_message,"
                "assistant_message_id,output_summary FROM agent_run WHERE id=%s",
                (int(run_id),),
            )
        assistant_row = None
        if run_row and run_row.get("assistant_message_id"):
            assistant_row = row_query(
                state["db"],
                "SELECT id,content,tokens,truncated,citations FROM message WHERE id=%s",
                (int(run_row["assistant_message_id"]),),
            )

        manifest = parse_manifest(run_row.get("context_manifest") if run_row else None)
        manifest_check = validate_manifest(manifest, snapshot)

        citations = (call.get("end") or {}).get("citations")
        if citations is None and assistant_row:
            raw_citations = assistant_row.get("citations")
            if isinstance(raw_citations, str):
                try:
                    citations = json.loads(raw_citations)
                except json.JSONDecodeError:
                    citations = None
            else:
                citations = raw_citations
        citation_check = validate_citations(citations, state["db"], space_id)

        stream_diag = (diagnostics or {}).get("stream") or {}
        usage = stream_diag.get("usage")
        usage_status = (diagnostics or {}).get("stream", {}).get("usageStatus")
        if usage_status is None:
            usage_status = "REAL" if usage else "UNKNOWN"
        if not usage:
            usage = "UNKNOWN"

        finish_reason = stream_diag.get("finishReason")
        truncated = bool((call.get("end") or {}).get("truncated"))
        problems: list[str] = []
        if call.get("error"):
            problems.append(f"SSE error：{call['error']}")
        if call.get("empty"):
            problems.append("回答为空")
        if truncated or finish_reason == "length":
            problems.append(f"回答截断：finishReason={finish_reason}")
        if run_id is None or run_row is None:
            problems.append("run_id 无法关联 agent_run")
        elif str(run_row.get("status")) not in {"COMPLETED", "TRUNCATED"}:
            problems.append(f"agent_run 状态异常：{run_row.get('status')}")
        if usage_status not in {"REAL", "UNKNOWN"}:
            problems.append(f"Usage 状态非 REAL/UNKNOWN：{usage_status}")
        problems.extend(manifest_check["problems"])
        problems.extend(citation_check["problems"])
        if HISTORY_MARKER not in call["answer"]:
            problems.append("回答未包含预置历史唯一标记")
        if context_record is None:
            problems.append("未取得 Context Hash（未见新的 LLM 调用记录）")
        elif int(context_record.get("fixtureMarkerOccurrences") or 0) <= 0:
            problems.append("送入模型的上下文不含预置历史唯一标记")

        post_freeze = run_script("freeze_check.py", str(snapshot_path), "--check", timeout=120)
        post_qdrant = run_script(
            "qdrant_freeze_check.py", str(snapshot_path), "--check", timeout=120
        )
        results["steps"].extend([post_freeze, post_qdrant])
        if post_freeze["exitCode"] != 0:
            problems.append("运行后 MySQL freeze_check 不一致")
        if post_qdrant["exitCode"] != 0:
            problems.append("运行后 Qdrant freeze_check 不一致")

        run_record = {
            "index": index,
            "strategy": "C",
            "question": NEUTRAL_QUESTION,
            "runId": run_id,
            "clientRequestId": call["clientRequestId"],
            "providerRequestId": stream_diag.get("providerRequestId"),
            "providerRequestUuid": stream_diag.get("requestId"),
            "httpStatus": call["httpStatus"],
            "finishReason": finish_reason,
            "truncated": truncated,
            "usage": usage,
            "usageStatus": usage_status,
            "latencyMs": call["elapsedMs"],
            "diagnosticsLatencyMs": stream_diag.get("latencyMs"),
            "contextStrategy": (context_after or {}).get("contextStrategy"),
            "agentRunStatus": run_row.get("status") if run_row else None,
            "manifest": manifest,
            "manifestCheck": manifest_check,
            "citations": citations,
            "citationCheck": citation_check,
            "contextHash": context_record,
            "newContextRecords": new_records,
            "embeddingCalls": embedding_calls,
            "embeddingTexts": embedding_texts,
            "answerChars": call["answerChars"],
            "answerContainsFixtureMarker": HISTORY_MARKER in call["answer"],
            "answerPreview": call["answer"][:200],
            "events": call["events"],
            "problems": problems,
        }

        # 每次结束 restore，并再次校验（任务要求）。
        restore_after = run_script(
            "restore_snapshot.py",
            str(snapshot_path),
            "--apply",
            "--prune-run-messages",
            "--json-out",
            str(Path(state["tempDir"]) / f"{label}-restore-after.json"),
            timeout=180,
        )
        results["steps"].append(restore_after)
        if restore_after["exitCode"] != 0:
            problems.append("运行后 restore/freeze 原子性检查失败")
        final_freeze = run_script("freeze_check.py", str(snapshot_path), "--check", timeout=120)
        final_qdrant = run_script(
            "qdrant_freeze_check.py", str(snapshot_path), "--check", timeout=120
        )
        results["steps"].extend([final_freeze, final_qdrant])
        if final_freeze["exitCode"] != 0:
            problems.append("restore 后 MySQL freeze_check 不一致")
        if final_qdrant["exitCode"] != 0:
            problems.append("restore 后 Qdrant freeze_check 不一致")

        run_record["restoreAfter"] = {
            "exitCode": restore_after["exitCode"],
            "freezeCheck": final_freeze["exitCode"],
            "qdrantCheck": final_qdrant["exitCode"],
        }
        results["runs"].append(run_record)
        print(json.dumps(
            {
                "runId": run_id,
                "finishReason": finish_reason,
                "usageStatus": usage_status,
                "contextStrategy": run_record["contextStrategy"],
                "manifestProblems": manifest_check["problems"],
                "citationProblems": citation_check["problems"],
                "problems": problems,
            },
            ensure_ascii=False,
            indent=2,
        ))

        if problems:
            failure = f"{label} 命中失败判据"
            print("FAIL：" + "；".join(problems), file=sys.stderr)
            break

    memory_after = memory_collection_state(state)
    results["defaultMemory"]["after"] = memory_after
    if memory_before != memory_after:
        failure = failure or "默认 Memory collection 在运行前后发生变化"
        results["problems"].append("默认 Memory collection 被读写")

    if failure:
        results["problems"].append(failure)
        results["passed"] = False
    else:
        results["passed"] = len(results["runs"]) == args.runs

    results["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    results_path = (
        Path(args.results)
        if args.results
        else Path(state["tempDir"]) / "c-verification-results.json"
    )
    write_json(results_path, results)
    print(f"\n结果已写入 {results_path}")

    if args.cleanup and results["passed"]:
        cleanup = run_script(PILOT_DIR / "cleanup.py", args.state, timeout=300)
        results["steps"].append(cleanup)
        results["cleanup"] = cleanup
        write_json(results_path, results)

    return 0 if results["passed"] else 1


def self_test() -> dict[str, Any]:
    problems: list[str] = []
    snapshot = {
        "dataset": {"revisionId": 88, "evidenceId": 99},
    }
    manifest = {
        "intent": "LEARNING_REVIEW",
        "sources": [
            {
                "type": "trajectory",
                "source_id": "revision:88",
                "retrieval_status": "SUCCESS",
                "injection_status": "SELECTED",
                "reason": "in_budget",
                "estimated_tokens": 10,
            },
            {
                "type": "trajectory_source",
                "source_id": "evidence:99",
                "retrieval_status": "SUCCESS",
                "injection_status": "SELECTED",
                "reason": "in_budget",
                "estimated_tokens": 10,
            },
        ],
        "budget": {
            "per_type": {
                "trajectory": {"used": 20},
                "evidence": {"used": 0},
            }
        },
    }
    check = validate_manifest(manifest, snapshot)
    if check["problems"]:
        problems.extend(check["problems"])
    if not check["revisionEntry"] or not check["evidenceEntry"]:
        problems.append("合法 Manifest 未被识别")

    broken = json.loads(json.dumps(manifest))
    broken["sources"] = broken["sources"][:1]
    if not validate_manifest(broken, snapshot)["problems"]:
        problems.append("缺少 evidence entry 未被检出")

    unbalanced = json.loads(json.dumps(manifest))
    unbalanced["sources"][0]["estimated_tokens"] = 20
    if not validate_manifest(unbalanced, snapshot)["problems"]:
        problems.append("token 拆分不守恒/不均衡未被检出")

    knowledge = {
        "intent": "KNOWLEDGE_QUERY",
        "sources": [
            {
                "type": "evidence",
                "source_id": "evidence:99",
                "retrieval_status": "SUCCESS",
                "injection_status": "SELECTED",
                "reason": "in_budget",
                "estimated_tokens": 10,
            }
        ],
        "budget": {"per_type": {"evidence": {"used": 10}}},
    }
    if validate_manifest(knowledge, snapshot)["problems"]:
        problems.append("KNOWLEDGE_QUERY 下不应要求 revision entry")

    return {"problems": problems}


if __name__ == "__main__":
    sys.exit(main())
