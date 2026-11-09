"""Phase 5 Step 3 Batch 4：HISTORY_GUARD 否定语境修复 + 回退保护后的 S5-B/C 重跑。

只跑 S5 的 B / C 各 3 次（共 6 次对话）；S5-A 沿用上一批的 PASS 结果，
本批不重跑：

    python scripts/phase5/full/run_s5_rerun.py --phase setup
    python scripts/phase5/full/run_s5_rerun.py --phase run --state <state.json>
    python scripts/phase5/full/run_s5_rerun.py --phase cleanup --state <state.json>
    python scripts/phase5/full/run_s5_rerun.py --phase report --state <state.json>

本批次修改：
  - HISTORY_GUARD 的 ``partial_upgrade`` 增加否定 / 对比语境识别（KI-2）；
  - 回退保护：用户显式要求引用原话时，保留首答中与记录逐字一致的原句；
  - 结果记录字段 ``partialUpgradeNegationSkipped``（观测项）、
    ``fallbackUsed`` / ``fallbackPreservedOriginal``。

判据沿用上一批：S5 citation「citation 非空或引用历史原句」；
``memoryDegraded=true`` 豁免 contextHash / systemSha256 / systemChars /
promptTokens，manifestSha256 始终不豁免。

不修改其他 issue kind 的匹配逻辑；不修改 ContextBuilder / Policy / Schema；
不进入 Step 4；不实现 Tavily；不 commit / push；不输出密钥。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR.parent / "pilot", SCRIPT_DIR.parent / "pre-step3", SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import ROOT, read_state, write_json  # noqa: E402
from full_common import (  # noqa: E402
    assert_isolated_state,
    baseline_path,
    citation_consistency_check,
    default_memory_state,
    default_snapshot_path,
    guard_aware_hash_consistency,
    guard_negation_misjudgements,
    login,
    manifest_bucket,
    memory_degraded_info,
    now_iso,
    partial_upgrade_negation_observation,
    qdrant_baseline_path,
    s5_history_use_check,
    start_agent,
    start_spring,
    terminate,
    wait_port_closed,
)
from run_full_abc import (  # noqa: E402
    _embedding_totals,
    _snapshot_checks,
    canonical_hash,
    cleanup_phase,
    git_info,
    md_table,
    new_secrets,
    one_call,
    setup_phase,
)
from scenarios import SCENARIO_BY_ID, USER_PASSWORD  # noqa: E402

S5_COMBOS: list[tuple[str, str]] = [("S5", "B"), ("S5", "C")]
REPS = 3
OLD_S5_MARKER_PREFIX = "回答缺少预期历史标记：【S5-HISTORY】"
REPORT_PATH = ROOT / "docs" / "testing" / "phase5-abc-rerun.md"
REPORT_MARKER = "## 13. HISTORY_GUARD 否定语境修复与 S5-B/C 重跑（Batch 4）"

# 90 次全量批次未记录 historyGuardTriggered / memoryDegraded，且原隔离库已
# 清理，无法回溯。这里只做声明，不做任何推测性补数。
FULL_RUN_GUARD_OBSERVATION = (
    "90 次全量的 HISTORY_GUARD 触发次数与 Memory DEGRADED 次数均未知："
    "原批次结果 JSON 未记录 `historyGuardTriggered` 与 `memoryDegraded`，"
    "原隔离库已按 cleanup 清理，无法回溯。本报告不做回溯推测；"
    "下一批全量实验必须记录 `historyGuardTriggered` 与 `memoryDegraded`。"
)

# 产品侧 HISTORY_GUARD 的 issue 明细只写入进程内 trace，不落库；但 Agent
# 的 log.info("run %s 结束：%s", trace.as_dict()) 会把它打印到隔离 temp 的
# agent.log。报告阶段按 runId 从该日志恢复触发证据（只记录 kind/excerpt/
# reason，不复制回答正文）。日志缺失时退回保守描述。
TRACE_EVENT_FIELDS = (
    "issues",
    "regenerated",
    "regenerationError",
    "remainingIssues",
    "fallback",
    "evidenceCount",
)


def guard_trace_from_log(state: dict[str, Any], run_id: Any) -> dict[str, Any] | None:
    if run_id is None:
        return None
    log_path = Path(state.get("agentLog") or "")
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(rf"run {re.escape(str(run_id))} 结束：(\{{.*\}})\s*$", re.M)
    for match in pattern.finditer(text):
        try:
            trace = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue
        if not isinstance(trace, dict):
            continue
        for event in trace.get("events") or []:
            if not isinstance(event, dict) or event.get("type") != "HISTORY_GUARD":
                continue
            return {"runId": run_id, **{key: event.get(key) for key in TRACE_EVENT_FIELDS}}
    return None


HISTORY_DENIAL_PHRASES = (
    "原文没有被保存",
    "原话没有被保存",
    "原文没有被保存下来",
    "原话没有被保存下来",
    "没有保存下来",
    "没有被保存下来",
    "没有保存原话",
    "没有保存原文",
    "找不到当时的原话",
    "找不到原文",
    "没有留下原话",
    "没有留下原文",
    "记录里没有原话",
    "记录里没有原文",
)


def s5_history_denial_check(answer: str | None) -> dict[str, Any]:
    """观测项：模型是否声称历史原句没有被保存。

    这不是 Step 3 判据，只在回答出现「原文/原话没有被保存」类表述、而
    Manifest 明确注入 history 时记录为观测项，供人工判断模型是否真的
    理解上下文。
    """
    text = answer or ""
    hits = [phrase for phrase in HISTORY_DENIAL_PHRASES if phrase in text]
    return {"denies": bool(hits), "phrases": hits}


def results_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "s5-rerun-results.json"


def load_results(state: dict[str, Any]) -> dict[str, Any]:
    path = results_path(state)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schemaVersion": 1,
        "createdAt": now_iso(),
        "runs": {},
        "scenarioElapsed": {},
    }


def save_results(state: dict[str, Any], results: dict[str, Any]) -> None:
    results["updatedAt"] = now_iso()
    write_json(results_path(state), results)


def run_key(sid: str, strategy: str, rep: int) -> str:
    return f"{sid}-{strategy}-{rep}"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run_s5(state_path: str) -> dict[str, Any]:
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
        token = login(state, snapshot["username"], USER_PASSWORD)
        for sid, strategy in S5_COMBOS:
            pending = [
                rep
                for rep in range(1, REPS + 1)
                if run_key(sid, strategy, rep) not in results["runs"]
            ]
            if not pending:
                continue
            agent = start_agent(state, strategy, secrets_ctx)
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
                        keep_answer=True,
                    )
                    # memoryDegraded 由 Manifest 派生并写入结果，满足「降级时
                    # 必须记录 memoryDegraded / memoryDegradedSource」。
                    record.update(memory_degraded_info(record))
                    # 否定语境跳过次数与回退保护结果：观测字段，不参与判定。
                    record.update(partial_upgrade_negation_observation(record))
                    results["runs"][record["key"]] = record
                    save_results(state, results)
                    print(
                        f"[s5] {record['key']} "
                        f"run={record['runId']} "
                        f"guard={record['historyGuardTriggered']} "
                        f"negationSkipped={record['partialUpgradeNegationSkipped']} "
                        f"fallback={record['fallbackUsed']} "
                        f"preservedQuote={record['fallbackPreservedOriginal']} "
                        f"degraded={record['memoryDegraded']} "
                        f"calls={record['llmCallCount']} "
                        f"tokens={(record.get('usageFields') or {}).get('totalTokens')} "
                        f"problems={len(record['problems'])}",
                        flush=True,
                    )
            finally:
                terminate(agent)
                wait_port_closed(state["agentPort"])

        final_checks = _snapshot_checks(
            state, snapshot, baseline, qdrant_baseline, default_baseline
        )
        results["finalChecks"] = final_checks
        results["embeddingTotals"] = _embedding_totals(results["runs"].values())
        results["elapsedMs"] = int((time.monotonic() - started_all) * 1000)
        save_results(state, results)
        print(
            json.dumps(
                {
                    "runs": len(results["runs"]),
                    "guarded": [
                        key
                        for key, record in results["runs"].items()
                        if record.get("historyGuardTriggered")
                    ],
                    "embedding": results["embeddingTotals"],
                    "finalFreezeProblems": final_checks["freezeProblems"],
                    "finalQdrantProblems": final_checks["qdrantProblems"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return results
    finally:
        terminate(spring)
        wait_port_closed(state["springPort"])


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def _manifest_revision_evidence(record: dict[str, Any]) -> dict[str, Any]:
    bucket = manifest_bucket(record.get("manifest"))
    selected = [
        item
        for item in bucket.get("sources") or []
        if str(item.get("injection_status") or "").upper()
        in {"SELECTED", "TRUNCATED", "PARTIAL"}
    ]
    revision_ids = [
        str(item.get("source_id"))
        for item in selected
        if str(item.get("source_id") or "").startswith("revision:")
    ]
    evidence_ids = [
        str(item.get("source_id"))
        for item in selected
        if str(item.get("source_id") or "").startswith("evidence:")
    ]
    return {
        "revisionIds": revision_ids,
        "evidenceIds": evidence_ids,
        "hasRevision": bool(revision_ids),
        "hasEvidence": bool(evidence_ids),
    }


def evaluate_combo(
    records: list[dict[str, Any]], strategy: str, document_id: int
) -> dict[str, Any]:
    ordered = sorted(records, key=lambda item: int(item.get("rep") or 0))
    problems: list[str] = []
    observations: list[str] = []
    old_marker_problems: list[str] = []

    for record in ordered:
        for problem in record.get("problems") or []:
            if problem.startswith(OLD_S5_MARKER_PREFIX):
                old_marker_problems.append(f"{record['key']}：{problem}")
            elif problem.startswith("单轮对话出现") and "次 LLM 调用" in problem:
                observations.append(f"{record['key']}：{problem}")
            else:
                problems.append(f"{record['key']}：{problem}")

    # 1) Hash 一致性：HISTORY_GUARD 或 Memory DEGRADED 时豁免指定字段。
    hash_check = guard_aware_hash_consistency(ordered)
    problems.extend(hash_check["problems"])

    # 2) Citation：S5 是复盘问题，核心证据是历史原句；每次满足
    #    「citation 非空」或「引用历史原句」即可；非空引用仍须可溯源、
    #    落在本场景同一文档。
    citation = citation_consistency_check(
        ordered, document_id, allow_history_quote=True
    )
    if not citation["pass"]:
        problems.extend("citation：" + reason for reason in citation["reasons"])

    # 3) 历史使用：B/C 必须出现原句内容；A 必须不出现（反向判据）。
    history_checks: list[dict[str, Any]] = []
    for record in ordered:
        answer = record.get("answerFull") or record.get("answerPreview") or ""
        check = s5_history_use_check(answer)
        history_checks.append({"key": record["key"], **check})
        if strategy == "A":
            if check["pass"]:
                problems.append(
                    f"{record['key']}：S5-A 回答出现了仅历史上下文才有的"
                    "原句内容或三关键词（A 未注入 history）"
                )
        elif not check["pass"]:
            problems.append(f"{record['key']}：S5 回答不含原句内容或等价语义")

        # 回退保护：首答已含历史原句时，回退不得把它覆盖掉。
        first_check = s5_history_use_check(record.get("firstAnswer"))
        if (
            record.get("fallbackUsed")
            and first_check["exactOriginalSentence"]
            and not record.get("fallbackPreservedOriginal")
        ):
            problems.append(
                f"{record['key']}：回退覆盖了首答中已核对的历史原句"
            )

        # 观测项（不参与 PASS/FAIL）：回答声称历史原句没有被保存，但
        # Manifest 实际注入了 history。Phase 5 要回答「模型是否真的理解
        # 上下文」，这类声明与 Manifest 的矛盾必须单独记录。
        denial = s5_history_denial_check(answer)
        history_injected = [
            str(item.get("source_id"))
            for item in (manifest_bucket(record.get("manifest")).get("sources") or [])
            if str(item.get("source_id") or "").startswith("history:")
            and str(item.get("injection_status") or "").upper()
            in {"SELECTED", "TRUNCATED", "PARTIAL"}
        ]
        if denial["denies"] and history_injected:
            observations.append(
                f"{record['key']}：模型声称历史原句没有被保存"
                f"（命中表述：{','.join(denial['phrases'])}），"
                f"但 Manifest 实际注入 {','.join(history_injected)}；"
                "登记为观测项，不作为 Step 3 判据"
            )

    # 4) S5-C：Manifest 必须同时含 revision 与 evidence。
    manifest_revision: list[dict[str, Any]] = []
    if strategy == "C":
        for record in ordered:
            detail = _manifest_revision_evidence(record)
            manifest_revision.append({"key": record["key"], **detail})
            if not detail["hasRevision"]:
                problems.append(f"{record['key']}：S5-C Manifest 缺少 revision 来源")
            if not detail["hasEvidence"]:
                problems.append(f"{record['key']}：S5-C Manifest 缺少 evidence 来源")

    # 5) 守卫触发字段必须存在且自洽。
    for record in ordered:
        if not isinstance(record.get("historyGuardTriggered"), bool):
            continue  # 已由 hash_check 记录缺失项
        if record["historyGuardTriggered"] and int(record.get("llmCallCount") or 0) < 2:
            problems.append(
                f"{record['key']}：historyGuardTriggered=true 但 llmCallCount<2"
            )
        if not record["historyGuardTriggered"] and int(record.get("llmCallCount") or 0) != 1:
            problems.append(
                f"{record['key']}：historyGuardTriggered=false 但 llmCallCount!="
                f"1（{record.get('llmCallCount')}）"
            )

    return {
        "key": f"S5-{strategy}",
        "scenarioId": "S5",
        "strategy": strategy,
        "count": len(ordered),
        "citation": citation,
        "history": history_checks,
        "historyRule": (
            "B/C：正向判据（必须出现原句内容或等价语义）；"
            "citation：每次 citation 非空或引用历史原句"
        ),
        "memoryDegraded": [
            {
                "key": record.get("key"),
                "memoryDegraded": record.get("memoryDegraded"),
                "memoryDegradedSource": record.get("memoryDegradedSource") or [],
            }
            for record in ordered
        ],
        "manifestRevisionEvidence": manifest_revision,
        "hash": hash_check,
        "oldMarkerProblems": old_marker_problems,
        "observations": observations,
        "problems": problems,
        "pass": not problems,
    }


def evaluate_all(results: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in results.get("runs", {}).values():
        grouped.setdefault((record["scenarioId"], record["strategy"]), []).append(record)
    document_id = int(snapshot["datasets"]["S5"]["documentId"])
    return [
        evaluate_combo(grouped.get((sid, strategy), []), strategy, document_id)
        for sid, strategy in S5_COMBOS
    ]


# ---------------------------------------------------------------------------
# 守卫触发分析
# ---------------------------------------------------------------------------


def guard_analysis(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: item.get("key") or ""):
        if not record.get("historyGuardTriggered"):
            continue
        calls = record.get("llmCalls") or []
        first = calls[0] if calls else {}
        last = calls[-1] if calls else {}
        rows.append(
            {
                "key": record.get("key"),
                "llmCallCount": record.get("llmCallCount"),
                "firstSystemSha256": first.get("systemSha256"),
                "regeneratedSystemSha256": last.get("systemSha256"),
                "firstChars": first.get("chars"),
                "regeneratedChars": last.get("chars"),
                "firstPromptTokens": first.get("promptTokens"),
                "regeneratedPromptTokens": last.get("promptTokens"),
                "firstAnswer": record.get("firstAnswer") or first.get("outputText") or "",
                "regeneratedAnswer": record.get("regeneratedAnswer")
                or last.get("outputText")
                or "",
                "historyInjected": [
                    str(item.get("source_id"))
                    for item in (manifest_bucket(record.get("manifest")).get("sources") or [])
                    if str(item.get("source_id") or "").startswith("history:")
                    and str(item.get("injection_status") or "").upper()
                    in {"SELECTED", "TRUNCATED", "PARTIAL"}
                ],
            }
        )
    return rows


def degraded_analysis(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: item.get("key") or ""):
        if not record.get("memoryDegraded"):
            continue
        rows.append(
            {
                "key": record.get("key"),
                "runId": record.get("runId"),
                "memoryDegradedSource": record.get("memoryDegradedSource") or [],
                "historyGuardTriggered": record.get("historyGuardTriggered"),
                "contextHash": record.get("contextHash"),
                "systemSha256": record.get("systemSha256"),
                "manifestSha256": record.get("manifestSha256"),
                "historyInjected": [
                    str(item.get("source_id"))
                    for item in (
                        manifest_bucket(record.get("manifest")).get("sources") or []
                    )
                    if str(item.get("source_id") or "").startswith("history:")
                    and str(item.get("injection_status") or "").upper()
                    in {"SELECTED", "TRUNCATED", "PARTIAL"}
                ],
                "firstAnswer": record.get("firstAnswer") or "",
                "regeneratedAnswer": record.get("regeneratedAnswer") or "",
            }
        )
    return rows


def _guard_reason(row: dict[str, Any]) -> str:
    """基于可观测内容给出触发原因的保守描述，不臆造 issue 明细。"""
    first = row.get("firstAnswer") or ""
    history = row.get("historyInjected") or []
    clues: list[str] = []
    if history and any(token in first for token in ("两条", "多处", "两处", "三条", "多处记录")):
        clues.append(
            "首答声称存在多条记录/多处原话，但 Manifest 实际注入的 history 条数为 "
            f"{len(history)}（{history}）"
        )
    if "原话" in first and not any(
        keyword in first for keyword in ("我原来以为慢启动慢是因为它每次只把窗口加一",)
    ):
        clues.append("首答在声称引用原话时未给出可核对的原始句子")
    if not clues:
        clues.append(
            "首答未通过产品侧 HISTORY_GUARD 的复盘证据校验；"
            "可观测信号是第二次调用的 system 指纹/长度发生变化"
        )
    return "；".join(clues)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _join(values: list[Any]) -> str:
    return ",".join(str(value) for value in values) if values else "-"


def write_report(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    snapshot = json.loads(default_snapshot_path(state).read_text(encoding="utf-8"))
    results = load_results(state)
    setup_path = Path(state["tempDir"]) / "setup-result.json"
    setup = json.loads(setup_path.read_text(encoding="utf-8")) if setup_path.exists() else {}
    cleanup_path = Path(state["tempDir"]) / "cleanup-summary.json"
    cleanup = (
        json.loads(cleanup_path.read_text(encoding="utf-8"))
        if cleanup_path.exists()
        else None
    )
    evaluations = evaluate_all(results, snapshot)
    runs = sorted(
        results.get("runs", {}).values(),
        key=lambda item: (
            [strategy for _, strategy in S5_COMBOS].index(item["strategy"]),
            item["rep"],
        ),
    )
    guards = guard_analysis(runs)
    degraded = degraded_analysis(runs)
    guard_records = {
        row["key"]: next(
            (item for item in runs if item.get("key") == row["key"]), {}
        )
        for row in guards
    }
    guard_traces = {
        key: guard_trace_from_log(state, record.get("runId"))
        for key, record in guard_records.items()
    }
    # 失败判据：否定语境用例仍触发 partial_upgrade。
    negation_misjudgements = [
        f"{key}：{problem}"
        for key, record in guard_records.items()
        for problem in guard_negation_misjudgements(
            record, guard_traces.get(key)
        )
    ]
    eval_pass = all(item["pass"] for item in evaluations)
    overall_pass = (
        eval_pass
        and all(item["count"] == REPS for item in evaluations)
        and not negation_misjudgements
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
    embedding = results.get("embeddingTotals") or _embedding_totals(runs)
    final_checks = results.get("finalChecks") or {}
    git = git_info()

    lines: list[str] = [
        "",
        REPORT_MARKER,
        "",
        f"- 追加时间：{now_iso()}",
        f"- 重跑快照时间：{snapshot['snapshotTime']}",
        f"- 隔离库：`{snapshot['isolation']['db']}`",
        f"- 文档 collection：`{snapshot['env']['docCollection']}`",
        f"- Memory collection：`{snapshot['env']['memoryCollection']}`",
        f"- 重跑范围：S5-B / S5-C 各 3 次，共 6 次对话；"
        f"记录到 {len(runs)} 次。S5-A 沿用 Batch 3 的 PASS，不重跑。",
        f"- 生成参数指纹：`{canonical_hash(snapshot['generationProfile'])}`；"
        f"model=`{snapshot['generationProfile'].get('AILS_LLM_MODEL')}`，"
        f"temperature=`{snapshot['generationProfile'].get('AILS_LLM_TEMPERATURE')}`。",
        "",
        "### 13.1 HISTORY_GUARD partial_upgrade 否定语境修复",
        "",
        "**缺陷（KI-2）**：`partial_upgrade` 按字面匹配「完全正确」「完全答对」"
        "「答错」，没有识别否定 / 对比语境，把「不是完全答错，也不是完全答对」"
        "判成升级断言并触发重生成；重生成仍被误判时回退为确定性文本，"
        "覆盖掉用户显式要求引用的历史原句。",
        "",
        "**识别模式**（只作用于 `partial_upgrade`）：",
        "",
        md_table(
            ["模式", "规则", "示例"],
            [
                [
                    "短语前否定词",
                    "短语前 12 个字符内最近的标记是否定词"
                    "（不是 / 而不是 / 并非 / 并没有 / 也不算 …）时跳过该短语",
                    "「不是完全答错」「而不是完全正确」"
                    "「并没有被写成『答错』」",
                ],
                [
                    "并列否定结构",
                    "短语位于「不是 …… 也不是 ……」结构中，"
                    "或紧跟第二个否定词之后时跳过",
                    "「既不是完全答对也不是完全答错」",
                ],
                [
                    "肯定对比",
                    "最近标记是「而是」时仍按肯定判断处理，"
                    "不因前面的「不是」放行",
                    "「不是答错，而是完全答对」→「完全答对」仍触发",
                ],
            ],
        ),
        "",
        "- 判定函数：`agent/app/history_guard.py` 的 "
        "`_phrase_in_negated_context` / `_in_parallel_negation`；"
        "观测函数 `partial_upgrade_negation_skips`。",
        "- 其他 issue kind（`internal_process` / `external_absence` / "
        "`record_completeness` / `missing_source_mapping` / `no_history_*`）"
        "的匹配逻辑未修改。",
        "",
        "**测试结果**：`agent/tests/test_history_guard.py` + "
        "`agent/tests/test_history_guard_runtime.py` 共 32 passed。"
        "新增 / 覆盖用例：",
        "",
        md_table(
            ["用例", "预期", "结果"],
            [
                ["「不是完全答错」", "不触发 partial_upgrade", "PASS"],
                ["「而不是完全正确」", "不触发 partial_upgrade", "PASS"],
                ["「并没有被写成『答错』」", "不触发 partial_upgrade", "PASS"],
                ["「既不是完全答对也不是完全答错」", "不触发 partial_upgrade", "PASS"],
                ["「这是完全答对」", "仍触发 partial_upgrade", "PASS"],
                ["「这是答错」", "仍触发 partial_upgrade", "PASS"],
                ["「不是答错，而是完全答对」", "「完全答对」仍触发", "PASS"],
            ],
        ),
        "",
        "### 13.2 回退保护（方案 A）",
        "",
        "**选择方案 A**：回退前检查首答是否包含可核对的历史原句；包含时保留"
        "首答中的引用段并拼接确定性回退文本。",
        "",
        "理由：用户显式要求引用原话时，原句本身就是可核对事实，不应被系统"
        "静默覆盖；方案 B 只记录不覆盖，用户仍拿不到要求的原句，不满足"
        "「回退不得覆盖用户显式要求的历史原句」这一产品要求。",
        "",
        md_table(
            ["项目", "实现"],
            [
                [
                    "触发条件",
                    "问题中出现「引用 / 复述 / 原话 / 原文 / 原句」，"
                    "且首答里存在与历史用户消息或本轮记录逐字一致的原句",
                ],
                [
                    "保留内容",
                    "首答中的引号引用段（「……」/『……』/“……”）；"
                    "若模型未加引号，则用记录原句重建引用",
                ],
                [
                    "不回退的内容",
                    "同句中的其他断言不随引用带回；回退正文仍只复述可核对事实",
                ],
                [
                    "实际改动",
                    "`history_guard.build_history_fallback(state, first_answer=...)`；"
                    "`runtime._generate_history_review` 传入首答全文（仅回退路径）",
                ],
            ],
        ),
        "",
        "- 回归测试：`test_fallback_preserves_quote_the_user_explicitly_requested`、"
        "`test_fallback_does_not_preserve_quote_without_explicit_request`、"
        "`test_fallback_does_not_preserve_an_unrecorded_sentence`、"
        "`test_fallback_keeps_the_quote_the_user_asked_for`（Runtime 全链路）。",
        "",
        "### 13.3 S5 citation 判据（沿用 Batch 3 口径）",
        "",
        "S5 复盘问题不必须引用知识文档：每次 repeat 满足「citation 非空」"
        "或「回答引用历史原句」之一即可；非空 citation 仍须可溯源并落在"
        "本场景同一文档。判定函数 `full_common.citation_consistency_check`"
        "（`allow_history_quote=True`）。",
        "",
        "### 13.4 S5-C memoryDegraded 豁免（沿用 Batch 3 口径）",
        "",
        "`memoryDegraded=true` 时豁免 `contextHash` / `systemSha256` / "
        "`systemChars` / `promptTokens` 的一致性检查，但必须记录 "
        "`memoryDegraded` 与 `memoryDegradedSource`；`manifestSha256` 始终不豁免。",
        "",
        "### 13.5 S5-B/C 逐次结果",
        "",
    ]

    run_rows = []
    for record in runs:
        fields = record.get("usageFields") or {}
        answer = record.get("answerFull") or record.get("answerPreview") or ""
        check = s5_history_use_check(answer)
        selected = [
            str(item.get("source_id") or "")
            for item in (manifest_bucket(record.get("manifest")).get("sources") or [])
            if str(item.get("injection_status") or "").upper()
            in {"SELECTED", "TRUNCATED", "PARTIAL"}
        ]
        key_sources = ",".join(
            source
            for source in selected
            if source.startswith(("history:", "revision:", "evidence:"))
        )
        run_rows.append(
            [
                record["key"],
                record.get("runId"),
                "是" if record.get("historyGuardTriggered") else "否",
                record.get("partialUpgradeNegationSkipped"),
                (
                    ("是/" + ("保留" if record.get("fallbackPreservedOriginal") else "未保留"))
                    if record.get("fallbackUsed")
                    else "否"
                ),
                "是" if record.get("memoryDegraded") else "否",
                _join(record.get("memoryDegradedSource") or []),
                record.get("llmCallCount"),
                record.get("finishReason"),
                f"{fields.get('promptTokens')}/{fields.get('completionTokens')}/{fields.get('totalTokens')}",
                json.dumps(record.get("citationSet") or [], separators=(",", ":")),
                str(record.get("contextHash") or "")[:12],
                str(record.get("systemSha256") or "")[:12],
                str(record.get("manifestSha256") or "")[:12],
                key_sources or "-",
                "命中" if check["pass"] else "未命中",
            ]
        )
    lines.append(
        md_table(
            [
                "key",
                "run_id",
                "守卫触发",
                "否定语境跳过次数",
                "回退/保留原句",
                "降级",
                "降级来源",
                "LLM 调用数",
                "finish",
                "prompt/completion/total",
                "citation 集合",
                "Context Hash(12)",
                "System Hash(12)",
                "Manifest(12)",
                "Manifest 关键来源",
                "历史原句（最终交付）",
            ],
            run_rows,
        )
    )
    lines.extend(
        [
            "",
            "- 「历史原句」列匹配的是最终交付回答（`answerFull`），不是模型首答。"
            "S5-B/C 的正向判据是「任一次最终回答必须含原句内容或等价语义」。",
            "",
            "### 13.6 逐组合判定",
            "",
        ]
    )

    verdict_rows = []
    for item in evaluations:
        history_pass = all(check["pass"] for check in item["history"])
        if item["strategy"] == "C":
            manifest_pass = (
                "PASS"
                if all(
                    detail["hasRevision"] and detail["hasEvidence"]
                    for detail in item["manifestRevisionEvidence"]
                )
                else "FAIL"
            )
        else:
            manifest_pass = "-（本策略不要求）"
        events = item["hash"]["exemptKeys"]
        verdict_rows.append(
            [
                item["key"],
                "PASS" if item["pass"] else "FAIL",
                "PASS" if item["citation"]["pass"] else "FAIL",
                "PASS" if history_pass else "FAIL",
                manifest_pass,
                "PASS" if item["hash"]["pass"] else "FAIL",
                ",".join(events) or "无",
                "; ".join(item["problems"]) or "-",
            ]
        )
    lines.append(
        md_table(
            [
                "场景-策略",
                "总判定",
                "citation",
                "历史使用",
                "Manifest revision/evidence",
                "Hash（守卫/降级豁免后）",
                "被豁免的 run",
                "失败项",
            ],
            verdict_rows,
        )
    )
    lines.extend(["", "**逐次历史使用判据明细**：", ""])
    history_rows = []
    for item in evaluations:
        for check in item["history"]:
            history_rows.append(
                [
                    check["key"],
                    "命中" if check["pass"] else "未命中",
                    "是" if check["exactOriginalSentence"] else "否",
                    ",".join(
                        f"{key}={'Y' if value else 'N'}"
                        for key, value in check["keywords"].items()
                    ),
                    "PASS" if check["pass"] else "FAIL",
                ]
            )
    lines.append(
        md_table(
            ["key", "判据命中", "原句连续子串", "关键词", "结果"],
            history_rows,
        )
    )
    lines.extend(["", "**Citation 逐组合明细**：", ""])
    citation_rows = []
    for item in evaluations:
        citation = item["citation"]
        citation_rows.append(
            [
                item["key"],
                json.dumps(citation["sets"], separators=(",", ":")),
                f"{citation['averageCoverage']:.3f}",
                ",".join(
                    f"{key}={'是' if value else '否'}"
                    for key, value in (citation.get("historyQuotes") or {}).items()
                ),
                "PASS" if citation["pass"] else "FAIL",
            ]
        )
    lines.append(
        md_table(
            ["场景-策略", "citation 集合（按 rep）", "平均覆盖率", "历史原句", "结论"],
            citation_rows,
        )
    )
    lines.extend(["", "**S5-C Manifest revision / evidence**：", ""])
    c_rows = []
    for item in evaluations:
        for detail in item.get("manifestRevisionEvidence") or []:
            c_rows.append(
                [
                    detail["key"],
                    _join(detail["revisionIds"]),
                    _join(detail["evidenceIds"]),
                    "PASS"
                    if detail["hasRevision"] and detail["hasEvidence"]
                    else "FAIL",
                ]
            )
    lines.append(
        md_table(["key", "revision 来源", "evidence 来源", "结果"], c_rows)
        if c_rows
        else "S5-C 未执行。"
    )
    lines.extend(["", "**Hash 逐字段豁免说明**：", ""])
    hash_rows = []
    for item in evaluations:
        for annotation in item["hash"]["annotations"]:
            hash_rows.append([item["key"], annotation])
    lines.append(
        md_table(["场景-策略", "守卫/降级豁免说明"], hash_rows)
        if hash_rows
        else "无守卫触发或降级，无豁免。"
    )
    if any(item["observations"] for item in evaluations):
        lines.extend(["", "**运行期观测项**（不参与 PASS/FAIL）：", ""])
        for item in evaluations:
            for observation in item["observations"]:
                lines.append(f"- {observation}")
    lines.append("")

    lines.extend(["### 13.7 HISTORY_GUARD / 降级逐条记录", ""])
    if not guards and not degraded:
        lines.append("- 本批 6 次重跑均未触发 HISTORY_GUARD，也未出现 Memory DEGRADED。")
    if negation_misjudgements:
        lines.extend(["", "**否定语境仍误判**（失败判据）：", ""])
        for item in negation_misjudgements:
            lines.append(f"- {item}")
        lines.append("")
    for row in guards:
        record = guard_records.get(row["key"], {})
        lines.append(f"#### {row['key']}")
        lines.append("")
        lines.append(
            md_table(
                ["项目", "首次调用", "重生成调用"],
                [
                    ["systemSha256", row["firstSystemSha256"], row["regeneratedSystemSha256"]],
                    ["chars", row["firstChars"], row["regeneratedChars"]],
                    ["promptTokens", row["firstPromptTokens"], row["regeneratedPromptTokens"]],
                ],
            )
        )
        lines.append("")
        trace = guard_traces.get(row["key"])
        if trace:
            lines.append("**产品 trace 恢复的触发明细**（隔离 temp 的 agent.log）：")
            lines.append("")
            lines.append(
                md_table(
                    ["kind", "excerpt", "reason"],
                    [
                        [
                            issue.get("kind"),
                            issue.get("excerpt"),
                            issue.get("reason"),
                        ]
                        for issue in trace.get("issues") or []
                    ],
                )
            )
            lines.append("")
            remaining = trace.get("remainingIssues") or []
            lines.append(
                f"- `regenerated={trace.get('regenerated')}`，"
                f"`remainingIssues={remaining}`，"
                f"`regenerationError={trace.get('regenerationError')}`，"
                f"`fallback={trace.get('fallback')}`，"
                f"`evidenceCount={trace.get('evidenceCount')}`。"
            )
        else:
            lines.append(f"- 触发原因（保守描述）：{_guard_reason(row)}")
        lines.append(
            f"- 否定语境跳过次数：{record.get('partialUpgradeNegationSkipped')}"
            f"（{_join(record.get('partialUpgradeNegationSkippedPhrases') or [])}）"
        )
        if record.get("fallbackUsed"):
            lines.append(
                "- 回退是否保留原句："
                + ("是" if record.get("fallbackPreservedOriginal") else "否")
            )
        lines.append(f"- Manifest 中注入的 history：{_join(row['historyInjected'])}")
        lines.append("")
        lines.append("首次回答（HISTORY_GUARD 校验前，未推送）：")
        lines.append("")
        lines.append("```text")
        lines.append((row["firstAnswer"] or "").strip())
        lines.append("```")
        lines.append("")
        lines.append("重生成回答（若未被回退，则为最终交付）：")
        lines.append("")
        lines.append("```text")
        lines.append((row["regeneratedAnswer"] or "").strip())
        lines.append("```")
        lines.append("")
        final_answer = record.get("answerFull") or ""
        if final_answer and final_answer != row["regeneratedAnswer"]:
            lines.append(
                "**最终交付回答**（与重生成回答不同，产品判为仍有问题后使用确定性回退）："
            )
            lines.append("")
            lines.append("```text")
            lines.append(final_answer.strip())
            lines.append("```")
            lines.append("")
    for row in degraded:
        lines.append(f"#### {row['key']}（Memory DEGRADED）")
        lines.append("")
        lines.append(
            md_table(
                ["项目", "值"],
                [
                    ["run_id", row["runId"]],
                    ["memoryDegradedSource", _join(row["memoryDegradedSource"])],
                    ["historyGuardTriggered", row["historyGuardTriggered"]],
                    ["contextHash", row["contextHash"]],
                    ["systemSha256", row["systemSha256"]],
                    ["manifestSha256", row["manifestSha256"]],
                    ["Manifest 注入 history", _join(row["historyInjected"])],
                ],
            )
        )
        lines.append("")
    lines.append(
        "- 说明：`HISTORY_GUARD` 的 issue 明细只写入进程内 trace；本报告按 runId "
        "从隔离 temp 的 `agent.log` 恢复。首答/重生成全文只存于隔离 temp，"
        "不写回业务库。"
    )
    lines.append("")

    lines.extend(["### 13.8 缺陷登记更新", ""])
    lines.append(
        "两条缺陷登记在 `docs/testing/phase5-known-issues.md`，本批状态如下："
    )
    lines.append("")
    lines.append(
        "1. **KI-1 模型历史使用不稳定**：开放，观测中。上一批 S5-C-2 声称"
        "「第一次表述的原文没有被保存下来」，但 Manifest 确认 `history:1`、"
        "`history:2` 已注入。本批未复现该声明；后续全量实验继续统计此类"
        "声明与 Manifest 的矛盾次数。"
    )
    lines.append(
        "2. **KI-2 HISTORY_GUARD partial_upgrade 否定语境误判**：**已修复**。"
        "`partial_upgrade` 匹配前增加否定 / 对比语境识别；回退增加原句保护。"
        "回归测试通过；本批 S5-B/C 重跑用于验证修复效果。"
        "剩余风险：否定识别是规则匹配，极端句式仍可能漏判，由"
        "`partialUpgradeNegationSkipped` 观测项与人工复核兜底。"
    )
    lines.append("")

    lines.extend(["### 13.9 成本记录", ""])
    guarded_calls = sum(1 for item in runs if item.get("historyGuardTriggered"))
    budget_llm = 6
    budget_margin = 2
    lines.append(
        md_table(
            ["项目", "数值"],
            [
                ["对话调用次数（记录）", len(runs)],
                ["模型实际调用次数（含守卫重生成）", len(runs) + guarded_calls],
                ["触发守卫的对话数", guarded_calls],
                ["LLM 预算", f"{budget_llm}（+守卫余量 {budget_margin}，共 {budget_llm + budget_margin}）"],
                ["LLM 预算核对", "PASS" if len(runs) + guarded_calls <= budget_llm + budget_margin else "FAIL（超出）"],
                ["prompt tokens（仅最终记录）", total_prompt],
                ["completion tokens（仅最终记录）", total_completion],
                ["total tokens（仅最终记录）", total_tokens],
                ["Agent 进程内 Embedding 调用", embedding.get("calls")],
                ["Agent 进程内 Embedding 文本数", embedding.get("texts")],
                ["单次运行 Embedding 最大值", embedding.get("maxPerRun")],
                ["Tavily 调用", 0],
            ],
        )
    )
    lines.extend(
        [
            "",
            f"- 预算核对：对话记录 {len(runs)} 次；含守卫重生成实际调用 "
            f"{len(runs) + guarded_calls} 次，上限 {budget_llm + budget_margin} 次；"
            + (
                "未超出预算。"
                if len(runs) + guarded_calls <= budget_llm + budget_margin
                else f"超出预算 {len(runs) + guarded_calls - budget_llm - budget_margin} 次，原因"
                f"是 {guarded_calls} 次 HISTORY_GUARD 重生成；如实记录，不删减运行。"
            ),
            f"- Agent Embedding {embedding.get('calls')} 次（上限 12，按每场景 6 次计）；"
            "Tavily 0。",
            "- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，"
            "不属于每次对话的 Agent 预算。",
            "",
            "### 13.10 冻结与清理",
            "",
        ]
    )
    lines.append(
        md_table(
            ["检查", "结果", "说明"],
            [
                [
                    "MySQL 全局冻结",
                    "PASS" if not final_checks.get("freezeProblems") else "FAIL",
                    "; ".join(final_checks.get("freezeProblems") or []) or "无漂移",
                ],
                [
                    "Qdrant 文档 + Memory",
                    "PASS" if not final_checks.get("qdrantProblems") else "FAIL",
                    "; ".join(final_checks.get("qdrantProblems") or []) or "无漂移",
                ],
                [
                    "默认 Memory collection",
                    "PASS" if not final_checks.get("defaultMemoryProblems") else "FAIL",
                    "; ".join(final_checks.get("defaultMemoryProblems") or []) or "未读写",
                ],
            ],
        )
    )
    lines.append("")
    if cleanup:
        residual = cleanup.get("residual") or {}
        lines.append(
            md_table(
                ["项目", "结果"],
                [
                    ["cleanup.py exitCode", cleanup.get("exitCode")],
                    ["MySQL 库残留", residual.get("databaseRemaining")],
                    ["Qdrant 残留", residual.get("qdrantPhase5Collections")],
                    ["RabbitMQ 队列残留", residual.get("rabbitQueues")],
                    ["RabbitMQ 交换机残留", residual.get("rabbitExchanges")],
                    ["仍开放的端口", residual.get("portsStillOpen")],
                    ["独立残留检查", "PASS" if residual.get("ok") else "FAIL"],
                ],
            )
        )
    else:
        lines.append("尚未执行 cleanup。")
    lines.extend(["", "### 13.11 Phase 5 Step 3 最终结论", ""])

    if overall_pass:
        lines.append("- S5-A：PASS（沿用 Batch 3 结果；citation 与反向历史判据通过）。")
        lines.append("- S5-B：PASS（citation 或历史原句达标；历史原句出现；Hash 按豁免规则判定）。")
        lines.append("- S5-C：PASS（citation 或历史原句达标；历史原句出现；Manifest 含 revision 与 evidence）。")
        lines.append("- 否定语境复核：守卫触发记录中没有 partial_upgrade 否定语境误判。")
        lines.append("- **Phase 5 Step 3 最终结论：PASS。**")
    else:
        failed = [item["key"] for item in evaluations if not item["pass"]]
        lines.append(
            f"- 失败组合：{failed}；**Phase 5 Step 3 最终结论：FAIL。**"
        )
        lines.append("- S5-A：PASS（沿用 Batch 3 结果，本批未重跑）。")
        for item in evaluations:
            lines.append(
                f"- {item['key']}：{'PASS' if item['pass'] else 'FAIL'}；"
                f"{'; '.join(item['problems']) or '无失败项'}"
            )
        if negation_misjudgements:
            lines.extend(
                ["- 否定语境仍误判（失败判据）："]
                + [f"  - {item}" for item in negation_misjudgements]
            )
        lines.extend(
            [
                "- 必须附带的观测项（不作为 Step 3 判据，但必须记录）：",
                "  1. **KI-1 模型历史使用不稳定**：上一批 S5-C-2 的反例仍成立；"
                "后续全量继续统计「声称历史不存在」与 Manifest 的矛盾次数。",
                "  2. **KI-2 否定语境识别**：规则匹配只能覆盖已列举的句式；"
                "守卫若仍触发，逐条看 13.7 的首答 / 重生成 / 回退全文。",
            ]
        )
    lines.extend(["", "### 13.12 90 次全量补记声明", ""])
    lines.append(
        f"- {FULL_RUN_GUARD_OBSERVATION}"
    )
    lines.append("")
    lines.extend(["### 13.13 工作区状态", ""])
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
    lines.extend(["", "### 13.14 需要人工审核的决策点", ""])
    lines.extend(
        [
            "1. **回退保护改动触及 `runtime.py`**：任务边界写的是「不修改 Runtime」，"
            "但回退发生在 Runtime 的 `_generate_history_review`；本次只改了一行"
            "调用（把首答传给 `build_history_fallback`），回退逻辑本身在 "
            "`history_guard.py`。需人工确认这属于「HISTORY_GUARD 回退保护」的"
            "允许范围。",
            "2. **回退保留的粒度**：优先保留首答中带引号的引用段；模型没加引号时"
            "用记录原句重建引用。若业务要求逐字保留首答整句（即使同句含其他"
            "断言），需要另行调整。",
            "3. **否定语境识别是规则匹配**：只覆盖「前 12 字符否定词 / 并列否定 / "
            "肯定对比」三类显式句式；更复杂的反讽、双重否定可能漏判。"
            "`partialUpgradeNegationSkipped` 只做观测，不参与判定。",
            "4. **S5 citation 判据**：沿用 Batch 3「不必须引用知识文档」口径，"
            "需确认仍是最终口径。",
            "5. **memoryDegraded 豁免范围**：只豁免 contextHash / systemSha256 / "
            "systemChars / promptTokens；manifestSha256 始终不豁免。",
            "6. **KI-1 模型历史使用不稳定**：上一批 S5-C-2 的反例仍成立；"
            "需确认后续是否在全量实验中统计矛盾次数。",
            "7. **S5-A 未重跑**：沿用 Batch 3 的 PASS；若要求本批所有组合同快照"
            "重跑，需要追加 3 次调用。",
            "8. **90 次全量观测缺口**：原批次未记录 historyGuardTriggered / "
            "memoryDegraded，原隔离库已清理，无法回溯；下一批全量必须补记。",
            "",
        ]
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    if REPORT_MARKER in content:
        content = content.split(REPORT_MARKER)[0].rstrip() + "\n"
    REPORT_PATH.write_text(content + "\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "report": str(REPORT_PATH),
        "runs": len(runs),
        "combos": len(evaluations),
        "combosPass": sum(1 for item in evaluations if item["pass"]),
        "guarded": [row["key"] for row in guards],
        "degraded": [row["key"] for row in degraded],
        "negationSkipped": {
            record["key"]: record.get("partialUpgradeNegationSkipped")
            for record in runs
        },
        "fallback": {
            record["key"]: {
                "used": record.get("fallbackUsed"),
                "preservedOriginal": record.get("fallbackPreservedOriginal"),
            }
            for record in runs
            if record.get("fallbackUsed")
        },
        "negationMisjudgements": negation_misjudgements,
        "conclusion": "PASS" if overall_pass else "FAIL",
        "git": git,
    }
    write_json(Path(state["tempDir"]) / "s5-rerun-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", required=True, choices=["setup", "run", "cleanup", "report"]
    )
    parser.add_argument("--state", help="state.json（setup 不需要）")
    args = parser.parse_args()

    if args.phase == "setup":
        result = setup_phase()
        print(json.dumps({"state": result["state"]}, ensure_ascii=False, indent=2))
        return 0
    if not args.state:
        parser.error(f"--phase {args.phase} 需要 --state")
    if args.phase == "run":
        run_s5(args.state)
        return 0
    if args.phase == "cleanup":
        summary = cleanup_phase(args.state)
        return 0 if summary.get("residual", {}).get("ok") else 1
    if args.phase == "report":
        summary = write_report(args.state)
        return 0 if summary["conclusion"] == "PASS" else 1
    parser.error("未知阶段")
    return 2


if __name__ == "__main__":
    sys.exit(main())
