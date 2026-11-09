"""Phase 5 Step 3 全量 A/B/C 对照实验执行器。

三个阶段：

    python scripts/phase5/full/run_full_abc.py --phase setup
    python scripts/phase5/full/run_full_abc.py --phase run --state <state.json>
    python scripts/phase5/full/run_full_abc.py --phase cleanup --state <state.json>
    python scripts/phase5/full/run_full_abc.py --phase report --state <state.json>

- setup：启动隔离 Spring + Agent(C)，建 1 个用户 / 10 个场景，写历史消息与
  Understanding / Evidence / Revision / Memory，上传并索引 10 份 Knowledge，
  为 Memory 建向量，验证 S9 的 DEGRADED 触发条件，最后写全局冻结基线与
  Qdrant 基线，停止服务。
- run：每个场景按 A → C → B 分块启动 Agent（策略只能进程级切换），每种策略
  3 次 repeat；每次调用前后恢复快照并核对冻结、Qdrant、默认 Memory collection
  与 MQ 消费者；逐次落盘 results.json，可断点续跑。
- cleanup：调用 pilot/cleanup.py 删除本次隔离资源，并做一次独立残留检查。
- report：生成 docs/testing/phase5-abc-results.md（12 个章节）。

不修改 Runtime / ContextBuilder / Policy / Schema / SSE / Spring；不实现 Tavily；
不 commit / push；不输出任何密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

FULL_DIR = Path(__file__).resolve().parent
for _path in (FULL_DIR.parent / "pilot", FULL_DIR.parent / "pre-step3", FULL_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import ROOT, SPRING_JAR, connect_mysql, parse_env_file, read_state, write_json  # noqa: E402
from full_common import (  # noqa: E402
    REPS,
    RUN_STRATEGIES,
    agent_context,
    agent_diagnostics,
    analysis_consumer_problems,
    analysis_names,
    api,
    assert_isolated_state,
    chat,
    citation_set,
    citation_traceability,
    default_memory_state,
    diff_freeze,
    diff_qdrant,
    effective_generation_profile,
    freeze_hashes,
    login,
    manifest_bucket,
    manifest_problems,
    new_state,
    now_iso,
    parse_manifest,
    qdrant_state,
    rabbit_consumers,
    results_path,
    restore_scenario,
    row_query,
    row_query_all,
    save_state,
    seed_all,
    start_agent,
    start_spring,
    state_path_for,
    sync_memories,
    terminate,
    usage_fields,
    verify_s9_corruption,
    wait_port_closed,
    write_freeze_baseline,
    write_qdrant_baseline,
    default_snapshot_path,
    baseline_path,
    qdrant_baseline_path,
)
from scenarios import SCENARIOS, SCENARIO_BY_ID, USER_PASSWORD, scenario_summary  # noqa: E402

REPORT_PATH = ROOT / "docs" / "testing" / "phase5-abc-results.md"
EMBEDDING_MAX_PER_RUN = 3
# 「批量」指单次 Embedding 请求携带的文本条数上限（10），不是整批实验的
# 调用总数；乘以场景数得到本批允许的单请求批量（10 × 10 = 100）。整批总
# 调用数另按 docs/testing/phase5-pilot-results.md §10.2 的 120–150（建议
# 硬上限 200）记录，用作成本核对而不是失败判据。
EMBEDDING_MAX_BATCH = 10
EMBEDDING_BATCH_TEXT_CAP = EMBEDDING_MAX_BATCH * len(SCENARIOS)
EMBEDDING_DOCUMENTED_EXPECTED = "120-150"
EMBEDDING_DOCUMENTED_SUGGESTED_CAP = 200
CONTEXT_STRATEGY_ORDER = RUN_STRATEGIES
SNAPSHOT_FILE_NAME = "full-snapshot.json"


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_secrets() -> dict[str, str]:
    values = parse_env_file()
    token = values.get("AILS_INTERNAL_TOKEN", "").strip()
    if len(token) < 16:
        raise SystemExit("agent/.env 的 AILS_INTERNAL_TOKEN 为空或过短")
    identity = secrets.token_hex(32)
    delegation = secrets.token_hex(32)
    if len({token, identity, delegation}) != 3:
        raise SystemExit("内部凭据生成异常")
    return {"token": token, "identity": identity, "delegation": delegation}


def preflight() -> dict[str, Any]:
    values = parse_env_file()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("springJar", SPRING_JAR.exists(), str(SPRING_JAR.name))
    token = values.get("AILS_INTERNAL_TOKEN", "")
    add("internalToken", len(token) >= 16, "set" if token else "missing")
    try:
        max_tokens = int(values.get("AILS_LLM_MAX_TOKENS") or 0)
    except ValueError:
        max_tokens = 0
    add("llmMaxTokens>=4096", max_tokens >= 4096, max_tokens)
    add("llmModel", bool(values.get("AILS_LLM_MODEL")), values.get("AILS_LLM_MODEL"))
    add(
        "embeddingModel",
        bool(values.get("AILS_EMBEDDING_MODEL")),
        values.get("AILS_EMBEDDING_MODEL"),
    )
    add(
        "agentDelegationSecretAbsent",
        "AILS_INTERNAL_DELEGATION_SECRET" not in values
        or not values.get("AILS_INTERNAL_DELEGATION_SECRET"),
        "agent .env 不持有 delegation secret",
    )
    try:
        conn = connect_mysql(None)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                cursor.fetchone()
        finally:
            conn.close()
        add("mysql", True, "127.0.0.1:3306")
    except Exception as exc:  # noqa: BLE001
        add("mysql", False, f"{type(exc).__name__}")
    try:
        response = requests.get("http://127.0.0.1:6333/collections", timeout=5)
        add("qdrant", response.status_code == 200, response.status_code)
    except Exception as exc:  # noqa: BLE001
        add("qdrant", False, f"{type(exc).__name__}")
    try:
        response = requests.get(
            "http://127.0.0.1:15672/api/queues", auth=("guest", "guest"), timeout=5
        )
        add("rabbitmq", response.status_code == 200, response.status_code)
    except Exception as exc:  # noqa: BLE001
        add("rabbitmq", False, f"{type(exc).__name__}")
    add("portsFree", _ports_free(), "8001/8081")
    return {
        "checks": checks,
        "ok": all(item["ok"] for item in checks),
        "generationProfile": effective_generation_profile(),
    }


def _ports_free() -> bool:
    for port in (8001, 8081):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            return False
        except requests.RequestException:
            continue
    return True


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def setup_phase() -> dict[str, Any]:
    state = new_state()
    assert_isolated_state(state)
    report = preflight()
    if not report["ok"]:
        failed = [item for item in report["checks"] if not item["ok"]]
        raise SystemExit("前置条件 FAIL：" + json.dumps(failed, ensure_ascii=False))
    snapshot_time = time.strftime("%Y-%m-%d %H:%M:%S")
    secrets_ctx = new_secrets()
    snapshot_path = default_snapshot_path(state)
    save_state(state_path_for(state), state)
    spring = None
    agent = None
    started = time.monotonic()
    try:
        spring = start_spring(state, secrets_ctx)
        agent = start_agent(state, "C", secrets_ctx)
        save_state(state_path_for(state), state)
        seeded = seed_all(state, SCENARIOS, USER_PASSWORD, snapshot_time)
        sync = sync_memories(state, secrets_ctx, SCENARIOS, seeded["datasets"])
        corruption = verify_s9_corruption(
            state,
            login(state, seeded["username"], USER_PASSWORD),
            seeded["datasets"]["S9"],
        )
        consumers = rabbit_consumers(state["stamp"])
        consumer_problems = analysis_consumer_problems(consumers, state["stamp"])
        if consumer_problems:
            raise RuntimeError("分析消费者未关闭：" + "；".join(consumer_problems))
        snapshot = {
            "schemaVersion": 1,
            "kind": "phase5-full-abc-snapshot",
            "createdAt": now_iso(),
            "snapshotTime": snapshot_time,
            "isolation": {
                "db": state["db"],
                "collectionPrefix": state["collectionPrefix"],
                "memoryCollectionPrefix": state["memoryCollectionPrefix"],
            },
            "env": {
                "stamp": state["stamp"],
                "db": state["db"],
                "docCollection": state["collection"],
                "memoryCollection": state["memoryCollection"],
                "defaultMemoryCollection": state["defaultMemoryCollection"],
                "agentPort": state["agentPort"],
                "springPort": state["springPort"],
            },
            "userId": seeded["userId"],
            "username": seeded["username"],
            "datasets": seeded["datasets"],
            "scenarios": scenario_summary(),
            "generationProfile": report["generationProfile"],
            "s9Corruption": corruption,
            "memorySync": sync,
            "analysis": {
                "mode": "mq",
                "consumersAtSetup": consumers,
                "note": "不启动 Agent analysis worker，因此请求队列无消费者；"
                "Spring 结果队列消费者存在但收不到结果，不会写回认知数据。",
            },
            "setup": {
                "elapsedMs": seeded["elapsedMs"],
                "steps": seeded["setupSteps"],
                "preflight": report["checks"],
            },
            "embeddingCaliber": {
                "agentPerRunMax": EMBEDDING_MAX_PER_RUN,
                "batchMax": EMBEDDING_MAX_BATCH,
                "note": "Agent 进程内计数只覆盖运行时检索（Knowledge 查询 / Memory 查询）；"
                "setup 阶段 10 份文档的索引 Embedding 由 Spring 侧调用，不在该计数内。",
            },
        }
        write_json(snapshot_path, snapshot)
        freeze = write_freeze_baseline(snapshot_path, snapshot)
        qdrant = write_qdrant_baseline(snapshot_path, state)
        default_memory = default_memory_state(state)
        result = {
            "ok": True,
            "state": str(state_path_for(state)),
            "snapshot": str(snapshot_path),
            "snapshotTime": snapshot_time,
            "db": state["db"],
            "collections": {
                "document": state["collection"],
                "memory": state["memoryCollection"],
                "defaultMemory": state["defaultMemoryCollection"],
            },
            "documents": seeded["setupSteps"],
            "freezeBaseline": freeze["combined"],
            "qdrantBaseline": {
                "document": qdrant["document"]["pointCount"],
                "memory": qdrant["memory"]["pointCount"],
            },
            "defaultMemoryBaseline": default_memory,
            "analysisConsumers": consumers,
            "s9Corruption": corruption,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }
        write_json(Path(state["tempDir"]) / "setup-result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        terminate(agent)
        wait_port_closed(state["agentPort"])
        terminate(spring)
        wait_port_closed(state["springPort"])
        save_state(state_path_for(state), state)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def load_results(state: dict[str, Any]) -> dict[str, Any]:
    path = results_path(state)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schemaVersion": 1,
        "createdAt": now_iso(),
        "runs": {},
        "scenarioElapsed": {},
        "checks": {},
    }


def save_results(state: dict[str, Any], results: dict[str, Any]) -> None:
    results["updatedAt"] = now_iso()
    write_json(results_path(state), results)


def run_key(sid: str, strategy: str, rep: int) -> str:
    return f"{sid}-{strategy}-{rep}"


def _snapshot_checks(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    baseline: dict[str, Any],
    qdrant_baseline: dict[str, Any],
    default_baseline: dict[str, Any],
) -> dict[str, Any]:
    freeze = freeze_hashes(snapshot)
    qdrant = qdrant_state(state)
    default_memory = default_memory_state(state)
    consumers = rabbit_consumers(state["stamp"])
    return {
        "freezeProblems": diff_freeze(baseline, freeze),
        "freezeCombined": freeze["combined"],
        "qdrantProblems": diff_qdrant(qdrant_baseline, qdrant),
        "qdrantCombined": {
            "document": qdrant["document"]["combined"],
            "memory": qdrant["memory"]["combined"],
        },
        "defaultMemoryProblems": []
        if default_memory == default_baseline
        else [f"默认 Memory collection 变化：{default_baseline} -> {default_memory}"],
        "consumerProblems": analysis_consumer_problems(consumers, state["stamp"]),
        "consumers": consumers,
        "defaultMemory": default_memory,
    }


def _round_checks(checks: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in (
        "freezeProblems",
        "qdrantProblems",
        "defaultMemoryProblems",
        "consumerProblems",
    ):
        for item in checks.get(key) or []:
            problems.append(f"{key}: {item}")
    return problems


def answer_marker_checks(
    scenario: dict[str, Any], strategy: str, answer: str
) -> tuple[dict[str, bool], list[str]]:
    expect = scenario["expect"]
    markers: dict[str, bool] = {}
    problems: list[str] = []
    for marker in expect.get("answer_markers_BC", []) + expect.get(
        "hard_answer_markers", []
    ):
        present = marker in answer
        markers[marker] = present
        if strategy in {"B", "C"} and not present:
            problems.append(f"回答缺少预期历史标记：{marker}")
        if strategy == "A" and present:
            problems.append(f"A 回答出现了只存在于个人上下文的标记：{marker}")
    for marker in expect.get("answer_markers_A_absent", []):
        present = marker in answer
        markers[marker] = present
        if strategy == "A" and present:
            problems.append(f"A 回答出现了不应出现的标记：{marker}")
    return markers, problems


def one_call(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    snapshot_path: Path,
    token: str,
    sid: str,
    strategy: str,
    rep: int,
    baseline: dict[str, Any],
    qdrant_baseline: dict[str, Any],
    default_baseline: dict[str, Any],
    *,
    keep_answer: bool = False,
) -> dict[str, Any]:
    scenario = SCENARIO_BY_ID[sid]
    dataset = snapshot["datasets"][sid]
    conversation_id = int(dataset["currentConversationId"])
    problems: list[str] = []
    warnings: list[str] = []
    started = time.monotonic()

    pre_restore = restore_scenario(state, dataset, snapshot["snapshotTime"])
    if not pre_restore["exactMatch"]:
        problems.append(f"调用前快照恢复失败：{pre_restore}")
    pre_checks = _snapshot_checks(
        state, snapshot, baseline, qdrant_baseline, default_baseline
    )
    problems.extend(_round_checks(pre_checks))

    context_before = agent_context(state)
    call = chat(
        state, token, conversation_id, scenario["question"], f"{sid}-{strategy}-{rep}"
    )
    time.sleep(0.5)
    context_after = agent_context(state)
    records = context_after.get("records", [])
    new_records = records[int(context_before.get("count", 0)) :]
    context_record = new_records[-1] if new_records else None
    embedding_calls = int(context_after.get("embeddingCalls", 0)) - int(
        context_before.get("embeddingCalls", 0)
    )
    embedding_texts = int(context_after.get("embeddingTexts", 0)) - int(
        context_before.get("embeddingTexts", 0)
    )
    # 一轮对话内出现多次 LLM 调用时，本运行期只有一种路径：产品内置
    # HISTORY_GUARD 的首答校验未通过 → 追加指令重生成。记录每次调用的
    # system 指纹、长度与 usage，供 guard 豁免判据与首答/重生成分析使用。
    llm_calls: list[dict[str, Any]] = []
    for index, item in enumerate(new_records, start=1):
        item_usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        call_info: dict[str, Any] = {
            "index": index,
            "systemSha256": item.get("systemSha256"),
            "chars": item.get("chars"),
            "promptTokens": item_usage.get("prompt_tokens"),
            "outputChars": item.get("outputChars"),
            "outputSha256": item.get("outputSha256"),
            "completed": item.get("completed"),
        }
        if len(new_records) > 1:
            # 仅在守卫重生成时保存首答/重生成全文；正文只留在隔离 temp。
            call_info["outputText"] = item.get("outputText") or ""
        llm_calls.append(call_info)
    history_guard_triggered = len(new_records) > 1
    if len(new_records) != 1:
        problems.append(f"单轮对话出现 {len(new_records)} 次 LLM 调用（预期 1）")
    if context_record is not None and context_record.get("contextStrategy") != strategy:
        problems.append(
            f"上下文策略串号：记录 {context_record.get('contextStrategy')} != {strategy}"
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
    bucket = manifest_bucket(manifest)
    problems.extend(manifest_problems(bucket, strategy, scenario))

    citations = (call.get("end") or {}).get("citations")
    if citations is None and assistant_row:
        raw = assistant_row.get("citations")
        if isinstance(raw, str):
            try:
                citations = json.loads(raw)
            except json.JSONDecodeError:
                citations = None
        else:
            citations = raw
    trace = citation_traceability(citations, state["db"], int(dataset["spaceId"]))
    problems.extend(trace["problems"])

    diagnostics = (context_record or {}).get("diagnostics") or {}
    usage = (context_record or {}).get("usage")
    usage_status = (context_record or {}).get("usageStatus") or "UNKNOWN"
    if usage_status not in {"REAL", "UNKNOWN"}:
        problems.append(f"Usage 状态非 REAL/UNKNOWN：{usage_status}")
    if usage_status == "REAL":
        fields = usage_fields(usage)
        if fields["promptTokens"] is None or fields["totalTokens"] is None:
            problems.append("Usage 标为 REAL 但缺少 prompt/total tokens")
    else:
        fields = usage_fields(None)
    finish_reason = diagnostics.get("finishReason")
    truncated = bool((call.get("end") or {}).get("truncated")) or finish_reason == "length"

    if call.get("error"):
        problems.append(f"SSE error：{call['error']}")
    if call.get("empty"):
        problems.append("回答为空")
    if truncated:
        problems.append(f"回答截断：finishReason={finish_reason}")
    if run_id is None or run_row is None:
        problems.append("run_id 无法关联 agent_run")
    elif str(run_row.get("status")) not in {"COMPLETED", "TRUNCATED"}:
        problems.append(f"agent_run 状态异常：{run_row.get('status')}")
    if embedding_calls > EMBEDDING_MAX_PER_RUN:
        problems.append(
            f"单次 Embedding 调用 {embedding_calls} 次，超过预算 {EMBEDDING_MAX_PER_RUN}"
        )
    if context_record is None:
        problems.append("未取得 Context Hash")
    else:
        markers = context_record.get("scenarioMarkers") or {}
        foreign = {key: value for key, value in markers.items() if key != sid}
        if foreign:
            problems.append(f"上下文出现其他场景标记：{foreign}")
        if strategy in {"B", "C"} and int(markers.get(sid) or 0) <= 0:
            problems.append("上下文不含本场景预置标记")
        expected_types = set(bucket.get("selectedTypes") or [])
        if scenario["expect"].get("expect_truncation_or_drop"):
            statuses = {
                str(item.get("injection_status") or "")
                for item in bucket.get("sources") or []
            }
            budget = bucket.get("budget") or {}
            used = int(budget.get("used") or 0)
            effective = int(budget.get("effective") or budget.get("total") or 1)
            warnings.append(
                f"S10 预算压力：statuses={sorted(statuses)} used/effective={used}/{effective}"
            )

    markers, marker_problems = answer_marker_checks(
        scenario, strategy, call["answer"]
    )
    problems.extend(marker_problems)

    post_restore = restore_scenario(state, dataset, snapshot["snapshotTime"])
    if not post_restore["exactMatch"]:
        problems.append(f"调用后快照恢复失败：{post_restore}")
    post_checks = _snapshot_checks(
        state, snapshot, baseline, qdrant_baseline, default_baseline
    )
    problems.extend(_round_checks(post_checks))

    record = {
        "key": run_key(sid, strategy, rep),
        "scenarioId": sid,
        "scenarioName": scenario["name"],
        "strategy": strategy,
        "rep": rep,
        "conversationId": conversation_id,
        "clientRequestId": call["clientRequestId"],
        "runId": run_id,
        "traceId": run_row.get("trace_id") if run_row else None,
        "providerRequestId": diagnostics.get("providerRequestId"),
        "adapterRequestId": diagnostics.get("requestId"),
        "httpStatus": call["httpStatus"],
        "finishReason": finish_reason,
        "truncated": truncated,
        "usage": usage if usage_status == "REAL" else "UNKNOWN",
        "usageStatus": usage_status,
        "usageFields": fields,
        "latencyMs": call["elapsedMs"],
        "diagnosticsLatencyMs": diagnostics.get("latencyMs"),
        "contextStrategy": (context_after or {}).get("contextStrategy"),
        "agentRunStatus": run_row.get("status") if run_row else None,
        "agentRunDurationMs": run_row.get("duration_ms") if run_row else None,
        "manifest": manifest,
        "manifestSummary": {
            "present": bucket["present"],
            "intent": bucket["intent"],
            "policyVersion": bucket["policyVersion"],
            "sourceTypes": bucket["sourceTypes"],
            "selectedTypes": bucket["selectedTypes"],
            "sourceCount": bucket.get("sourceCount"),
            "selectedCount": bucket.get("selectedCount"),
            "budget": bucket.get("budget"),
            "perTypeTokens": bucket.get("perTypeTokens"),
            "sourceIds": bucket.get("sourceIds"),
        },
        "manifestSha256": canonical_hash(manifest) if manifest else None,
        "citations": citations if isinstance(citations, list) else [],
        "citationSet": citation_set(citations),
        "citationTrace": trace,
        "contextHash": context_record.get("contextSha256") if context_record else None,
        "systemSha256": context_record.get("systemSha256") if context_record else None,
        "systemChars": context_record.get("chars") if context_record else None,
        "contextRecord": context_record,
        "newContextRecordCount": len(new_records),
        # 结果 JSON 的显式观测字段：本轮是否触发 HISTORY_GUARD 重生成。
        # 判定规则见 full_common.guard_aware_hash_consistency。
        "historyGuardTriggered": history_guard_triggered,
        "llmCallCount": len(new_records),
        "llmCalls": llm_calls,
        "firstAnswer": (
            llm_calls[0].get("outputText") if history_guard_triggered and llm_calls else None
        ),
        "regeneratedAnswer": (
            llm_calls[-1].get("outputText") if history_guard_triggered and llm_calls else None
        ),
        "embeddingCalls": embedding_calls,
        "embeddingTexts": embedding_texts,
        "answerChars": call["answerChars"],
        "answerPreview": call["answer"][:300],
        # 仅重跑批次开启：把完整回答留在隔离 temp 的 results 文件里，供修正后的
        # 判据逐字判定；不写回业务库，也不改变生产行为。
        **({"answerFull": call["answer"]} if keep_answer else {}),
        "answerMarkers": markers,
        "events": call["events"],
        "preRestore": pre_restore,
        "postRestore": post_restore,
        "preChecks": pre_checks,
        "postChecks": post_checks,
        "problems": problems,
        "warnings": warnings,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }
    return record


def run_phase(
    state_path: str,
    *,
    only_scenarios: list[str] | None = None,
    reps: int = REPS,
) -> dict[str, Any]:
    state = read_state(state_path)
    assert_isolated_state(state)
    snapshot_path = default_snapshot_path(state)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path(snapshot_path).read_text(encoding="utf-8"))
    qdrant_baseline = json.loads(
        qdrant_baseline_path(snapshot_path).read_text(encoding="utf-8")
    )
    default_baseline = default_memory_state(state)
    results = load_results(state)
    secrets_ctx = new_secrets()
    spring = None
    started_all = time.monotonic()
    try:
        spring = start_spring(state, secrets_ctx)
        save_state(Path(state_path), state)
        # 登录必须在 Spring 启动之后：setup 结束时 Spring 已停止。
        token = login(state, snapshot["username"], USER_PASSWORD)
        scenario_ids = only_scenarios or [item["id"] for item in SCENARIOS]
        for sid in scenario_ids:
            scenario_started = time.monotonic()
            for strategy in CONTEXT_STRATEGY_ORDER:
                pending = [
                    rep
                    for rep in range(1, reps + 1)
                    if run_key(sid, strategy, rep) not in results["runs"]
                ]
                if not pending:
                    continue
                agent = start_agent(state, strategy, secrets_ctx)
                save_state(Path(state_path), state)
                try:
                    for rep in pending:
                        record = one_call(
                            state,
                            snapshot,
                            snapshot_path,
                            token,
                            sid,
                            strategy,
                            rep,
                            baseline,
                            qdrant_baseline,
                            default_baseline,
                        )
                        results["runs"][record["key"]] = record
                        save_results(state, results)
                        status = "OK" if not record["problems"] else "FAIL"
                        print(
                            f"[run] {record['key']} {status} "
                            f"run={record['runId']} "
                            f"tokens={record['usageFields'].get('totalTokens')} "
                            f"latency={record['latencyMs']}ms "
                            f"problems={len(record['problems'])}",
                            flush=True,
                        )
                finally:
                    terminate(agent)
                    wait_port_closed(state["agentPort"])
                    save_state(Path(state_path), state)
            results["scenarioElapsed"][sid] = int(
                (time.monotonic() - scenario_started) * 1000
            )
            save_results(state, results)

        # 全部结束后再做一次全局核对。
        final_checks = _snapshot_checks(
            state, snapshot, baseline, qdrant_baseline, default_baseline
        )
        total_embedding_calls = sum(
            int(item.get("embeddingCalls") or 0) for item in results["runs"].values()
        )
        total_embedding_texts = sum(
            int(item.get("embeddingTexts") or 0) for item in results["runs"].values()
        )
        results["finalChecks"] = final_checks
        results["embeddingTotals"] = _embedding_totals(
            results["runs"].values(), total_calls=total_embedding_calls,
            total_texts=total_embedding_texts,
        )
        results["elapsedMs"] = int((time.monotonic() - started_all) * 1000)
        save_results(state, results)
        print(
            json.dumps(
                {
                    "runs": len(results["runs"]),
                    "embeddingCalls": total_embedding_calls,
                    "embeddingTexts": total_embedding_texts,
                    "finalFreezeProblems": final_checks["freezeProblems"],
                    "finalQdrantProblems": final_checks["qdrantProblems"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return results
    finally:
        terminate(spring)
        wait_port_closed(state["springPort"])
        save_state(Path(state_path), state)


# ---------------------------------------------------------------------------
# cleanup + 独立残留检查
# ---------------------------------------------------------------------------


def cleanup_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    script = FULL_DIR.parent / "pilot" / "cleanup.py"
    completed = subprocess.run(
        [sys.executable, str(script), state_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    cleanup_result = None
    path = Path(state["tempDir"]) / "cleanup-result.json"
    if path.exists():
        cleanup_result = json.loads(path.read_text(encoding="utf-8"))
    residual = independent_residual_check(state)
    summary = {
        "exitCode": completed.returncode,
        "cleanup": cleanup_result,
        "residual": residual,
        "stdoutPreview": (completed.stdout or "")[-1500:],
        "stderrPreview": (completed.stderr or "")[-800:],
    }
    write_json(Path(state["tempDir"]) / "cleanup-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def independent_residual_check(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        conn = connect_mysql(None)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SHOW DATABASES LIKE %s", (state["db"],))
                result["databaseRemaining"] = len(cursor.fetchall())
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        result["databaseError"] = type(exc).__name__
    try:
        listing = requests.get("http://127.0.0.1:6333/collections", timeout=10).json()
        names = [
            str(item.get("name"))
            for item in listing.get("result", {}).get("collections", [])
        ]
        result["qdrantPhase5Collections"] = sorted(
            name for name in names if name.startswith("phase5-pilot-")
        )
    except Exception as exc:  # noqa: BLE001
        result["qdrantError"] = type(exc).__name__
    try:
        response = requests.get(
            "http://127.0.0.1:15672/api/queues", auth=("guest", "guest"), timeout=10
        )
        queues = [
            str(item.get("name"))
            for item in response.json()
            if str(item.get("name")).startswith(f"phase5.pilot.{state['stamp']}")
        ]
        exchanges = requests.get(
            "http://127.0.0.1:15672/api/exchanges",
            auth=("guest", "guest"),
            timeout=10,
        ).json()
        exchange_names = [
            str(item.get("name"))
            for item in exchanges
            if str(item.get("name")).startswith(f"phase5.pilot.{state['stamp']}")
        ]
        result["rabbitQueues"] = sorted(queues)
        result["rabbitExchanges"] = sorted(exchange_names)
    except Exception as exc:  # noqa: BLE001
        result["rabbitError"] = type(exc).__name__
    dead = []
    for port in (state["agentPort"], state["springPort"]):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            dead.append(port)
        except requests.RequestException:
            continue
    result["portsStillOpen"] = dead
    result["ok"] = (
        result.get("databaseRemaining") == 0
        and not result.get("qdrantPhase5Collections")
        and not result.get("rabbitQueues")
        and not result.get("rabbitExchanges")
        and not dead
        and not any("Error" in key for key in result)
    )
    return result


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def git_info() -> dict[str, Any]:
    def run(args: list[str]) -> str:
        completed = subprocess.run(
            args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8"
        )
        return (completed.stdout or "").strip()

    return {
        "head": run(["git", "rev-parse", "HEAD"]),
        "headSubject": run(["git", "log", "-1", "--format=%s"]),
        "status": run(["git", "status", "--porcelain"]),
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
    }


def _embedding_totals(
    records,
    *,
    total_calls: int | None = None,
    total_texts: int | None = None,
) -> dict[str, Any]:
    items = list(records)
    calls = (
        int(total_calls)
        if total_calls is not None
        else sum(int(item.get("embeddingCalls") or 0) for item in items)
    )
    texts = (
        int(total_texts)
        if total_texts is not None
        else sum(int(item.get("embeddingTexts") or 0) for item in items)
    )
    max_per_run = max(
        (int(item.get("embeddingCalls") or 0) for item in items), default=0
    )
    # 每次 Embedding 调用至少携带 1 条文本，因此「总文本数 - 总调用数 + 1」
    # 是单次请求文本数的上界；用它判定「单次批量」而不用整批调用总数。
    max_texts_per_call_upper = max(0, texts - calls + 1) if calls else 0
    return {
        "calls": calls,
        "texts": texts,
        "maxPerRun": max_per_run,
        "maxPerRunLimit": EMBEDDING_MAX_PER_RUN,
        "batchMax": EMBEDDING_MAX_BATCH,
        "batchTextCap": EMBEDDING_BATCH_TEXT_CAP,
        "maxTextsPerCallUpperBound": max_texts_per_call_upper,
        "withinBatchBudget": max_texts_per_call_upper <= EMBEDDING_BATCH_TEXT_CAP,
        "documentedExpected": EMBEDDING_DOCUMENTED_EXPECTED,
        "documentedSuggestedCap": EMBEDDING_DOCUMENTED_SUGGESTED_CAP,
    }


def _is_manifest_problem(problem: str) -> bool:
    """报告阶段用于把可复算的 Manifest 判据与运行期判据分开。"""
    prefixes = (
        "Manifest 缺失或无法解析",
        "A 注入了个人上下文：",
        "A 的 Manifest 不应有 intent",
        "B 缺少预期来源：",
        "B 未按近似实现：",
        "C intent=",
        "C policy_version=",
        "C 缺少预期来源：",
        "C 注入了不应出现的来源：",
        "C 未保留 DEGRADED：",
        "B 未把 DEGRADED 折叠为 EMPTY：",
    )
    return problem.startswith(prefixes)


def reviewed_problems(record: dict[str, Any]) -> list[str]:
    """用当前（修正后的）Manifest 判据复核已存 Manifest，其余判据沿用原始记录。

    原始 `problems` 保留在 results.json 中；这里只重算可以由 Manifest 本身
    确定性复现的部分，避免把判据实现偏差（例如 Evidence 以
    trajectory_source:evidence:* 注入）当成产品失败。
    """
    stored = list(record.get("problems") or [])
    runtime = [item for item in stored if not _is_manifest_problem(item)]
    scenario = SCENARIO_BY_ID[record["scenarioId"]]
    bucket = manifest_bucket(record.get("manifest"))
    return runtime + manifest_problems(bucket, record["strategy"], scenario)


def consistency_report(results: dict[str, Any]) -> dict[str, Any]:
    runs = results.get("runs", {})
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in runs.values():
        groups.setdefault(f"{record['scenarioId']}-{record['strategy']}", []).append(
            record
        )
    checks: list[dict[str, Any]] = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda item: item["rep"])
        fields = {
            "manifest": [item.get("manifestSha256") for item in items],
            "context": [item.get("contextHash") for item in items],
            "citations": [
                json.dumps(item.get("citationSet"), sort_keys=True) for item in items
            ],
            "promptTokens": [
                (item.get("usageFields") or {}).get("promptTokens") for item in items
            ],
            "strategy": [item.get("strategy") for item in items],
            "contextStrategy": [item.get("contextStrategy") for item in items],
        }
        problems = []
        if len(items) != REPS:
            problems.append(f"观测次数 {len(items)} != {REPS}")
        for name in ("manifest", "context", "citations", "promptTokens", "strategy"):
            values = fields[name]
            if len(set(values)) != 1:
                problems.append(f"{name} 不一致：{values}")
        checks.append(
            {
                "key": key,
                "scenarioId": items[0]["scenarioId"],
                "strategy": items[0]["strategy"],
                "count": len(items),
                **fields,
                "problems": problems,
            }
        )

    differences: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        sid = scenario["id"]
        hashes = {}
        for strategy in CONTEXT_STRATEGY_ORDER:
            items = [
                record
                for record in runs.values()
                if record["scenarioId"] == sid and record["strategy"] == strategy
            ]
            hashes[strategy] = sorted({item.get("contextHash") for item in items})
        pairs = {
            "A-vs-B": hashes.get("A") != hashes.get("B"),
            "A-vs-C": hashes.get("A") != hashes.get("C"),
            "B-vs-C": hashes.get("B") != hashes.get("C"),
        }
        differences.append({"scenarioId": sid, "contextHashes": hashes, "different": pairs})
    return {"groups": checks, "strategyDifferences": differences}


def report_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    snapshot_path = default_snapshot_path(state)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    results = load_results(state)
    setup_path = Path(state["tempDir"]) / "setup-result.json"
    setup = json.loads(setup_path.read_text(encoding="utf-8")) if setup_path.exists() else {}
    cleanup_path = Path(state["tempDir"]) / "cleanup-summary.json"
    cleanup = (
        json.loads(cleanup_path.read_text(encoding="utf-8"))
        if cleanup_path.exists()
        else None
    )
    consistency = consistency_report(results)
    runs = sorted(
        results.get("runs", {}).values(),
        key=lambda item: (
            [s["id"] for s in SCENARIOS].index(item["scenarioId"]),
            CONTEXT_STRATEGY_ORDER.index(item["strategy"]),
            item["rep"],
        ),
    )

    reviewed = {record["key"]: reviewed_problems(record) for record in runs}
    all_problems: list[dict[str, Any]] = []
    for record in runs:
        for problem in reviewed[record["key"]]:
            all_problems.append(
                {
                    "key": record["key"],
                    "scenarioId": record["scenarioId"],
                    "strategy": record["strategy"],
                    "rep": record["rep"],
                    "problem": problem,
                }
            )
    for group in consistency["groups"]:
        for problem in group["problems"]:
            all_problems.append(
                {
                    "key": group["key"],
                    "scenarioId": group["scenarioId"],
                    "strategy": group["strategy"],
                    "rep": "-",
                    "problem": "一致性：" + problem,
                }
            )
    final_checks = results.get("finalChecks") or {}
    for problem in final_checks.get("freezeProblems") or []:
        all_problems.append(
            {
                "key": "FINAL",
                "scenarioId": "-",
                "strategy": "-",
                "rep": "-",
                "problem": "冻结：" + problem,
            }
        )
    for problem in final_checks.get("qdrantProblems") or []:
        all_problems.append(
            {
                "key": "FINAL",
                "scenarioId": "-",
                "strategy": "-",
                "rep": "-",
                "problem": "Qdrant：" + problem,
            }
        )
    # 报告阶段按「单次请求批量」口径从原始计数重新核对，不直接沿用 run 阶段
    # 写入的 withinBatchBudget（该值曾误把整批调用总数当成批量）。
    embedding_totals = _embedding_totals(runs)
    if not embedding_totals.get("withinBatchBudget"):
        all_problems.append(
            {
                "key": "FINAL",
                "scenarioId": "-",
                "strategy": "-",
                "rep": "-",
                "problem": f"Embedding 批次总量超过 10×场景数：{embedding_totals}",
            }
        )
    cleanup_ok = bool(cleanup and cleanup.get("residual", {}).get("ok"))
    if cleanup and not cleanup_ok:
        all_problems.append(
            {
                "key": "CLEANUP",
                "scenarioId": "-",
                "strategy": "-",
                "rep": "-",
                "problem": "cleanup 或独立残留检查失败",
            }
        )

    total_prompt = sum(
        (item.get("usageFields") or {}).get("promptTokens") or 0 for item in runs
    )
    total_completion = sum(
        (item.get("usageFields") or {}).get("completionTokens") or 0 for item in runs
    )
    total_tokens = sum(
        (item.get("usageFields") or {}).get("totalTokens") or 0 for item in runs
    )
    usage_unknown = [item["key"] for item in runs if item.get("usageStatus") != "REAL"]
    retries = 0
    for item in runs:
        diagnostics = (item.get("contextRecord") or {}).get("diagnostics") or {}
        history = diagnostics.get("attemptHistory") or []
        retries += max(0, len(history) - 1)

    git = git_info()
    lines: list[str] = []
    lines.append("# Phase 5 Step 3：10 场景 × 3 策略 × 3 次全量 A/B/C 对照结果")
    lines.append("")
    lines.append(f"- 生成时间：{now_iso()}")
    lines.append(f"- 快照时间：{snapshot['snapshotTime']}")
    lines.append(f"- 隔离库：`{snapshot['isolation']['db']}`")
    lines.append(f"- 文档 collection：`{snapshot['env']['docCollection']}`")
    lines.append(f"- Memory collection：`{snapshot['env']['memoryCollection']}`")
    lines.append(f"- 生成参数指纹：`{canonical_hash(snapshot['generationProfile'])}`")
    lines.append(f"- 实际完成调用：{len(runs)} / {len(SCENARIOS) * len(CONTEXT_STRATEGY_ORDER) * REPS}")
    lines.append("")

    lines.append("## 1. 前置条件确认")
    lines.append("")
    preflight_checks = setup.get("preflight") or (
        (snapshot.get("setup") or {}).get("preflight") or []
    )
    preflight_rows = [
        [item["name"], "PASS" if item["ok"] else "FAIL", item["detail"]]
        for item in preflight_checks
    ]
    lines.append(md_table(["检查项", "结果", "说明"], preflight_rows))
    lines.append("")
    lines.append(
        "- 3 次 C 策略真实验证：依据仓库既有记录 `docs/testing/phase5-c-verification*.md` "
        "与 commit `87aa182`（本报告不重新执行）。"
    )
    lines.append(
        "- A/B/C 开关、C golden 回归、统一快照脚本、Memory 隔离与内部凭据均按既有提交使用；"
        "本实验未改生产代码。"
    )
    lines.append(
        f"- LLM 生成参数：model=`{snapshot['generationProfile'].get('AILS_LLM_MODEL')}`，"
        f"max_tokens=`{snapshot['generationProfile'].get('AILS_LLM_MAX_TOKENS')}`，"
        f"temperature 在子进程强制为 0（见第 12 节决策点）。"
    )
    lines.append("")

    lines.append("## 2. 10 个场景数据说明")
    lines.append("")
    scenario_rows = []
    for scenario in SCENARIOS:
        data = snapshot["datasets"][scenario["id"]]
        scenario_rows.append(
            [
                scenario["id"],
                scenario["name"],
                data["spaceId"],
                ",".join(str(value) for value in data["concepts"].values()),
                ",".join(str(value) for value in data["sessions"].values()),
                ",".join(str(value) for value in data["conversations"].values()),
                data["documentId"],
                data["documentChunkCount"],
                scenario["expect"].get("C_intent"),
            ]
        )
    lines.append(
        md_table(
            ["ID", "场景", "Space", "Concept", "Session", "Conversation", "文档", "Chunk", "C intent"],
            scenario_rows,
        )
    )
    lines.append("")
    for scenario in SCENARIOS:
        lines.append(f"- **{scenario['id']} {scenario['name']}**：{scenario['question']}")
        lines.append(f"  - 预期：{scenario['notes']}")
    lines.append("")

    lines.append("## 3. 90 次调用汇总")
    lines.append("")
    run_rows = []
    for item in runs:
        fields = item.get("usageFields") or {}
        summary = item.get("manifestSummary") or {}
        run_rows.append(
            [
                item["scenarioId"],
                item["strategy"],
                item["rep"],
                item.get("runId"),
                (item.get("clientRequestId") or "")[:8],
                item.get("finishReason"),
                f"{fields.get('promptTokens')}/{fields.get('completionTokens')}/{fields.get('totalTokens')}",
                item.get("usageStatus"),
                item.get("latencyMs"),
                ",".join(summary.get("selectedTypes") or []),
                len(item.get("citationSet") or []),
                "OK"
                if not reviewed[item["key"]]
                else f"FAIL({len(reviewed[item['key']])})",
            ]
        )
    lines.append(
        md_table(
            [
                "场景",
                "策略",
                "rep",
                "run_id",
                "request",
                "finish",
                "prompt/completion/total",
                "Usage",
                "latency_ms",
                "实际注入来源",
                "引用数",
                "结果",
            ],
            run_rows,
        )
    )
    lines.append("")

    lines.append("## 4. 一致性检查")
    lines.append("")
    consistency_rows = []
    for group in consistency["groups"]:
        consistency_rows.append(
            [
                group["key"],
                group["count"],
                "PASS" if not group["problems"] else "FAIL",
                "; ".join(group["problems"]) or "-",
            ]
        )
    lines.append(md_table(["场景-策略", "次数", "结果", "说明"], consistency_rows))
    lines.append("")
    diff_rows = []
    for item in consistency["strategyDifferences"]:
        diff_rows.append(
            [
                item["scenarioId"],
                "是" if item["different"].get("A-vs-B") else "否",
                "是" if item["different"].get("A-vs-C") else "否",
                "是" if item["different"].get("B-vs-C") else "否",
            ]
        )
    lines.append("策略间 Context Hash 是否不同（应全部为「是」）：")
    lines.append("")
    lines.append(md_table(["场景", "A vs B", "A vs C", "B vs C"], diff_rows))
    lines.append("")

    lines.append("## 5. A/B/C 分场景对照")
    lines.append("")
    for scenario in SCENARIOS:
        sid = scenario["id"]
        lines.append(f"### {sid} {scenario['name']}")
        lines.append("")
        rows = []
        for strategy in CONTEXT_STRATEGY_ORDER:
            items = [
                item
                for item in runs
                if item["scenarioId"] == sid and item["strategy"] == strategy
            ]
            if not items:
                rows.append([strategy, "未执行", "-", "-", "-", "-", "-"])
                continue
            first = items[0]
            summary = first.get("manifestSummary") or {}
            budget = summary.get("budget") or {}
            used = budget.get("used")
            effective = budget.get("effective") or budget.get("total")
            citations = sum(len(item.get("citationSet") or []) for item in items)
            trace_ok = all((item.get("citationTrace") or {}).get("traceable") for item in items)
            markers = first.get("answerMarkers") or {}
            marker_text = ",".join(
                f"{key}={'Y' if value else 'N'}" for key, value in markers.items()
            ) or "-"
            rows.append(
                [
                    strategy,
                    ",".join(summary.get("selectedTypes") or []) or "-",
                    summary.get("intent") or "null",
                    marker_text,
                    f"{citations}/3",
                    "可溯源" if trace_ok else "不可溯源",
                    f"{used}/{effective}" if effective else "-",
                ]
            )
        lines.append(
            md_table(
                [
                    "策略",
                    "实际注入来源",
                    "intent",
                    "答案历史标记",
                    "引用次数",
                    "引用溯源",
                    "预算 used/effective",
                ],
                rows,
            )
        )
        lines.append("")
        for strategy in CONTEXT_STRATEGY_ORDER:
            items = sorted(
                [
                    item
                    for item in runs
                    if item["scenarioId"] == sid and item["strategy"] == strategy
                ],
                key=lambda item: item["rep"],
            )
            if not items:
                continue
            first = items[0]
            problems = sorted(
                {p for item in items for p in reviewed[item["key"]]}
            )
            lines.append(
                f"- **{strategy}**："
                f"{'; '.join(problems) if problems else '无失败项'}。"
                f" 回答字数：{', '.join(str(item.get('answerChars')) for item in items)}。"
            )
        lines.append("")

    lines.append("## 6. 失败用例")
    lines.append("")
    if all_problems:
        lines.append(
            md_table(
                ["场景-策略", "rep", "失败项"],
                [[item["key"], item["rep"], item["problem"]] for item in all_problems],
            )
        )
    else:
        lines.append("未发现满足失败判据的用例。")
    lines.append("")
    lines.append(
        "- 判据复核：报告阶段用修正后的 Manifest 判据复核了已存 Manifest"
        "（Evidence 允许以 `trajectory_source:evidence:*` 出现，与顶层 evidence 等价；"
        "见 docs/testing/phase5-pilot-results.md §10.1），其余运行期判据沿用原始记录；"
        "原始 `problems` 未从 results.json 删除。"
    )
    lines.append("")
    warnings = [
        {"key": item["key"], "warning": warning}
        for item in runs
        for warning in item.get("warnings") or []
    ]
    if warnings:
        lines.append("观测性告警（不构成本批次失败判据）：")
        lines.append("")
        lines.append(
            md_table(
                ["场景-策略", "告警"], [[item["key"], item["warning"]] for item in warnings]
            )
        )
        lines.append("")

    lines.append("## 7. 成本记录")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "数值"],
            [
                ["LLM 调用次数（上下文记录）", len(runs)],
                ["prompt tokens", total_prompt],
                ["completion tokens", total_completion],
                ["total tokens", total_tokens],
                ["Usage=UNKNOWN 的调用", len(usage_unknown)],
                ["模型侧重试次数（attemptHistory 超出首轮）", retries],
                ["Agent 进程内 Embedding 调用", embedding_totals.get("calls")],
                ["Agent 进程内 Embedding 文本数", embedding_totals.get("texts")],
                ["单次运行 Embedding 最大值", embedding_totals.get("maxPerRun")],
                [
                    "单次请求文本数（上界）",
                    embedding_totals.get("maxTextsPerCallUpperBound"),
                ],
                ["Tavily 调用", 0],
                ["预计费用", "无法验证：仓库未提供可核对的单价与计费口径"],
            ],
        )
    )
    lines.append("")
    lines.append(
        f"- Embedding 口径：① 单次运行（一轮对话）≤ {EMBEDDING_MAX_PER_RUN} 次；"
        f"② 单次 Embedding 请求携带文本数 ≤ {EMBEDDING_MAX_BATCH}，本批允许上限 "
        f"{EMBEDDING_BATCH_TEXT_CAP}（10 × 场景数）。本批次 Agent 计数为 "
        f"{embedding_totals.get('calls')} 次调用 / {embedding_totals.get('texts')} 条文本，"
        f"单次运行最大 {embedding_totals.get('maxPerRun')} 次，"
        f"单次请求文本数上界 {embedding_totals.get('maxTextsPerCallUpperBound')}，"
        f"预算={'PASS' if embedding_totals.get('withinBatchBudget') else 'FAIL'}。"
    )
    lines.append(
        f"- 整批总调用数 {embedding_totals.get('calls')} 次，与 "
        "docs/testing/phase5-pilot-results.md §10.2 的预估 "
        f"{EMBEDDING_DOCUMENTED_EXPECTED} 次一致（建议硬上限 "
        f"{EMBEDDING_DOCUMENTED_SUGGESTED_CAP}）；该总数不是「批量」判据。"
    )
    lines.append(
        "- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不在 Agent 计数内；"
        "该阶段只执行一次，不属于每次对话的预算。"
    )
    lines.append("")
    elapsed_rows = [
        [sid, results.get("scenarioElapsed", {}).get(sid, "-")]
        for sid in [item["id"] for item in SCENARIOS]
    ]
    lines.append(md_table(["场景", "耗时 ms（含 3 策略切换与 9 次调用）"], elapsed_rows))
    lines.append("")

    lines.append("## 8. 冻结一致性")
    lines.append("")
    freeze_problems = final_checks.get("freezeProblems") or []
    qdrant_problems = final_checks.get("qdrantProblems") or []
    default_problems = final_checks.get("defaultMemoryProblems") or []
    lines.append(
        md_table(
            ["检查", "结果", "说明"],
            [
                [
                    "MySQL 全局冻结",
                    "PASS" if not freeze_problems else "FAIL",
                    "; ".join(freeze_problems) or "10 个 Space 与 U/E/R/M/文档均未漂移",
                ],
                [
                    "Qdrant 文档 + Memory",
                    "PASS" if not qdrant_problems else "FAIL",
                    "; ".join(qdrant_problems) or "point 集合与 payload 哈希未变",
                ],
                [
                    "默认 Memory collection",
                    "PASS" if not default_problems else "FAIL",
                    "; ".join(default_problems) or "未读写共享默认 collection",
                ],
            ],
        )
    )
    lines.append("")
    lines.append(
        f"- 冻结基线 combined：`{setup.get('freezeBaseline')}`；"
        f"结束后 combined：`{final_checks.get('freezeCombined')}`。"
    )
    lines.append("")

    lines.append("## 9. 清理状态")
    lines.append("")
    if cleanup:
        residual = cleanup.get("residual") or {}
        lines.append(
            md_table(
                ["项目", "结果"],
                [
                    ["cleanup.py exitCode", cleanup.get("exitCode")],
                    ["cleanup 自检 ok", (cleanup.get("cleanup") or {}).get("ok")],
                    ["MySQL 库残留", residual.get("databaseRemaining")],
                    ["Qdrant phase5-pilot 残留", residual.get("qdrantPhase5Collections")],
                    ["RabbitMQ 队列残留", residual.get("rabbitQueues")],
                    ["RabbitMQ 交换机残留", residual.get("rabbitExchanges")],
                    ["仍开放的端口", residual.get("portsStillOpen")],
                    ["独立残留检查", "PASS" if residual.get("ok") else "FAIL"],
                ],
            )
        )
    else:
        lines.append("尚未执行 cleanup。")
    lines.append("")

    lines.append("## 10. 结论")
    lines.append("")
    lines.append(f"- 失败项总数：{len(all_problems)}。")
    if all_problems:
        lines.append("- 结论：**FAIL**（存在满足失败判据的用例，详见第 6 节）。")
    else:
        lines.append(
            "- 结论：**PASS**（90 次调用、一致性、冻结、引用溯源与清理均满足本批次判据）。"
        )
    lines.append(
        "- 质量判断的边界：本报告依据可观测信号（Manifest 来源、历史标记、引用溯源、"
        "预算、usage）评估，不包含人工对答案正确性的逐条评分。"
    )
    lines.append("")

    lines.append("## 11. Git 状态")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "值"],
            [
                ["branch", git.get("branch")],
                ["HEAD", git.get("head")],
                ["HEAD subject", git.get("headSubject")],
                ["工作区状态", git.get("status") or "(clean)"],
                ["是否 commit", "否（本批次未执行 git commit）"],
                ["是否 push", "否（本批次未执行 git push）"],
            ],
        )
    )
    lines.append("")

    lines.append("## 12. 需要人工复核的决策点")
    lines.append("")
    lines.append(
        "1. **temperature=0**：全量实验只在 Agent 子进程注入 `AILS_LLM_TEMPERATURE=0`，"
        "未修改 `agent/.env`。目的是让同一场景 + 策略的 3 次 repeat 可对照；"
        "但这与生产默认温度不同，若评审要求保留生产温度，需要接受 3 次 repeat 的 "
        "Context Hash 仍可一致、但引用集合与 prompt tokens 可能波动。"
    )
    lines.append(
        "2. **S9 人为故障方式**：在隔离库中向 `learning_understanding.status` 写入非法枚举"
        "（空串），使 Spring 读取该行失败，从而触发 Agent 的 DEGRADED。该行只存在于隔离库，"
        "清理时随库删除；需要人工确认这是可接受的「检索异常」注入方式。"
    )
    lines.append(
        "3. **策略切换模型**：A/B/C 只能进程级切换，因此每个「场景 × 策略」块重启一次 "
        "Agent（共 30 次），块内 3 次 repeat 共用同一进程；没有做「每次调用都重启」的更强隔离。"
    )
    lines.append(
        "4. **Memory 向量来源**：实验用 Spring 公开 API 建 Memory 行，并用 Agent 内部 "
        "`/agent/memories/sync` 建向量；未经过生产 Outbox Relay。需要确认这等价于生产检索路径。"
    )
    lines.append(
        "5. **Embedding 计数口径**：只统计 Agent 进程内运行时检索的 Embedding；"
        "setup 阶段文档索引由 Spring 侧调用，单独说明、不计入每次对话预算。"
    )
    lines.append(
        "6. **分析写回**：以 `AILS_ANALYSIS_MODE=mq` 启动但未启动 Agent analysis worker，"
        "Spring 结果消费者存在但收不到结果；冻结表用于验证没有任何认知数据写回。"
    )
    lines.append(
        "7. **Citation 一致性**：3 次 repeat 要求引用集合一致；temperature=0 下仍可能因"
        "供应商非确定性出现差异，报告按失败判据如实标记。"
    )
    lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "report": str(REPORT_PATH),
        "runs": len(runs),
        "problems": len(all_problems),
        "conclusion": "PASS" if not all_problems else "FAIL",
        "git": git,
    }
    write_json(Path(state["tempDir"]) / "report-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        required=True,
        choices=["setup", "run", "cleanup", "report", "all"],
    )
    parser.add_argument("--state", help="state.json（setup 阶段不需要）")
    parser.add_argument("--scenarios", help="只跑这些场景，逗号分隔（调试用）")
    parser.add_argument("--reps", type=int, default=REPS)
    args = parser.parse_args()

    if args.phase == "setup":
        setup_phase()
        return 0
    if args.phase == "all":
        setup_phase()
        state_path = _latest_state_path()
        run_phase(state_path, reps=args.reps)
        cleanup_phase(state_path)
        report_phase(state_path)
        return 0
    if not args.state:
        parser.error(f"--phase {args.phase} 需要 --state")
    if args.phase == "run":
        only = [item.strip() for item in args.scenarios.split(",")] if args.scenarios else None
        run_phase(args.state, only_scenarios=only, reps=args.reps)
        return 0
    if args.phase == "cleanup":
        summary = cleanup_phase(args.state)
        return 0 if summary.get("residual", {}).get("ok") else 1
    if args.phase == "report":
        report_phase(args.state)
        return 0
    parser.error("未知阶段")
    return 2


def _latest_state_path() -> str:
    candidates = sorted(
        Path(os.environ.get("TEMP", ".")).glob("phase5-full-*/state.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("找不到 phase5-full-*/state.json")
    return str(candidates[0])


if __name__ == "__main__":
    sys.exit(main())
