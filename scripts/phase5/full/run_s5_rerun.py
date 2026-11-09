"""Phase 5 Step 3：S5 数据重设计 + HISTORY_GUARD 判据调整后的 S5-A/B/C 重跑。

只跑 S5 的 A / C / B 各 3 次（共 9 次对话）：

    python scripts/phase5/full/run_s5_rerun.py --phase setup
    python scripts/phase5/full/run_s5_rerun.py --phase run --state <state.json>
    python scripts/phase5/full/run_s5_rerun.py --phase cleanup --state <state.json>
    python scripts/phase5/full/run_s5_rerun.py --phase report --state <state.json>

本批次只修改：
  - S5 预置 Understanding（措辞与历史原句不重叠，见 scenarios.py）；
  - 结果记录字段 historyGuardTriggered / llmCallCount / llmCalls；
  - Hash 一致性判据：只豁免 HISTORY_GUARD 触发的运行（见
    full_common.guard_aware_hash_consistency）。

不修改问题、其他场景、Runtime / ContextBuilder / Policy / Schema；
不进入 Step 4；不实现 Tavily；不 commit / push；不输出密钥。
"""

from __future__ import annotations

import argparse
import json
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
    login,
    manifest_bucket,
    now_iso,
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

S5_COMBOS: list[tuple[str, str]] = [("S5", "A"), ("S5", "C"), ("S5", "B")]
REPS = 3
OLD_S5_MARKER_PREFIX = "回答缺少预期历史标记：【S5-HISTORY】"
REPORT_PATH = ROOT / "docs" / "testing" / "phase5-abc-rerun.md"
REPORT_MARKER = "## 11. S5 数据重设计与 HISTORY_GUARD 判据调整（追加）"

# 90 次全量批次未记录该字段，且原隔离库已清理，无法回溯。这里只做声明，
# 不做任何推测性补数。
FULL_RUN_GUARD_OBSERVATION = (
    "90 次全量的 HISTORY_GUARD 触发次数未知：原批次结果 JSON 未记录 "
    "`historyGuardTriggered`，原隔离库已按 cleanup 清理，无法回溯。"
    "本报告不做回溯推测，建议下一批全量实验补记该字段。"
)

# 产品侧 HISTORY_GUARD 的 issue 明细只写入进程内 trace，不落库。
# 本批在隔离 temp 的 agent.log 中保留了 trace 文本，以下是从该日志
# 逐字恢复的触发证据（只记录 kind/excerpt/reason，不复制回答正文）。
GUARD_TRACE_EVIDENCE: dict[str, dict[str, Any]] = {
    "S5-B-3": {
        "runId": 17,
        "traceEvent": "HISTORY_GUARD",
        "issues": [
            {
                "kind": "partial_upgrade",
                "excerpt": "完全答对",
                "reason": "记录里没有完全答对的证据",
            },
            {
                "kind": "partial_upgrade",
                "excerpt": "答错",
                "reason": "记录只判定为部分正确，不能改写成答错",
            },
        ],
        "regenerated": True,
        "regenerationError": None,
        "remainingIssues": [],
        "fallback": False,
        "evidenceCount": 1,
    }
}


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
                    results["runs"][record["key"]] = record
                    save_results(state, results)
                    print(
                        f"[s5] {record['key']} "
                        f"run={record['runId']} "
                        f"guard={record['historyGuardTriggered']} "
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

    # 1) Hash 一致性：守卫触发时只豁免指定字段。
    hash_check = guard_aware_hash_consistency(ordered)
    problems.extend(hash_check["problems"])

    # 2) Citation：S5-A/C/B 都要求可溯源、同一文档、覆盖率达标。
    citation = citation_consistency_check(ordered, document_id)
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
            "A：反向判据（不得出现原句/三关键词）；B/C：正向判据（必须出现）"
        ),
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
    eval_pass = all(item["pass"] for item in evaluations)
    overall_pass = eval_pass and all(item["count"] == REPS for item in evaluations)

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
    scenario = SCENARIO_BY_ID["S5"]

    lines: list[str] = [
        "",
        REPORT_MARKER,
        "",
        f"- 追加时间：{now_iso()}",
        f"- 重跑快照时间：{snapshot['snapshotTime']}",
        f"- 隔离库：`{snapshot['isolation']['db']}`",
        f"- 文档 collection：`{snapshot['env']['docCollection']}`",
        f"- Memory collection：`{snapshot['env']['memoryCollection']}`",
        f"- 重跑范围：S5-A / S5-C / S5-B 各 3 次，共 9 次对话；"
        f"记录到 {len(runs)} 次。",
        f"- 生成参数指纹：`{canonical_hash(snapshot['generationProfile'])}`；"
        f"model=`{snapshot['generationProfile'].get('AILS_LLM_MODEL')}`，"
        f"temperature=`{snapshot['generationProfile'].get('AILS_LLM_TEMPERATURE')}`。",
        "",
        "### 11.1 S5 预置 Understanding 重设计",
        "",
        "旧版 Understanding 与历史原句在措辞上高度重叠，导致模型引用 Understanding "
        "时无法区分「是否使用了历史」。本批次只改 S5 的 Understanding，"
        "历史原句、问题、其他场景与判据匹配规则均未修改。",
        "",
    ]

    lines.append(
        md_table(
            ["字段", "旧版（会与历史原句混淆）", "新版（与历史原句可区分）"],
            [
                [
                    "summary",
                    "【S5-U】用户原先误以为慢启动每次只加一；现已理解慢启动从 cwnd=1 开始指数增长。",
                    "【S5-U】用户曾把慢启动理解为线性增长，现已理解它是指数增长。",
                ],
                [
                    "misconceptions",
                    "曾认为慢启动每次只把窗口加一",
                    "曾把慢启动的增长方式理解为线性递增",
                ],
                [
                    "explanation",
                    "【S5-U】我原来以为慢启动是每次加一，其实是起点小。",
                    "【S5-U】我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。",
                ],
                [
                    "历史原句（不变）",
                    "【S5-HISTORY】我原来以为慢启动慢是因为它每次只把窗口加一。",
                    "【S5-HISTORY】我原来以为慢启动慢是因为它每次只把窗口加一。",
                ],
            ],
        )
    )
    lines.extend(
        [
            "",
            "**可判定性论证**：",
            "",
            "1. 判据的匹配对象是原句连续子串"
            "「我原来以为慢启动慢是因为它每次只把窗口加一」，"
            "或「慢启动」「每次只」「加一」三关键词同时出现。",
            "2. 新版 Understanding 的 summary / misconceptions / explanation "
            "均不含「每次只」，也不含原句的连续子串，"
            "因此引用 Understanding 不会命中正向判据。",
            "3. 历史原句本身仍逐字保留在 history 中；B/C 回答引用它时必然命中。",
            "4. 未改动 `s5_history_use_check` 的匹配规则，只改数据。",
            "",
            "**未改动的部分**：S5 的 revisions、memory、question 与其他场景数据均未修改。"
            "revision 文本中仍可能出现「每次只」等词，因此正向关键词判据在理论上"
            "仍可能被 revision 引用命中；这一点列为人工审核决策点。",
            "",
            "### 11.2 HISTORY_GUARD 判据调整",
            "",
            md_table(
                [
                    "项目",
                    "原判据",
                    "调整后判据",
                ],
                [
                    [
                        "记录字段",
                        "仅通过「单轮出现 2 次 LLM 调用」间接看出",
                        "结果 JSON 增加 `historyGuardTriggered`（布尔）、"
                        "`llmCallCount`、`llmCalls`（逐次 system 指纹/长度/usage）",
                    ],
                    [
                        "Hash 一致性",
                        "3 次 `contextHash` / `promptTokens` 必须完全一致",
                        "未触发守卫的 repeat 之间必须一致；"
                        "`historyGuardTriggered=true` 的 repeat 豁免 "
                        "`contextHash` / `systemSha256` / `systemChars` / `promptTokens`，"
                        "但必须在结果中逐条标注",
                    ],
                    [
                        "Manifest",
                        "3 次一致",
                        "保持 3 次一致（不豁免）",
                    ],
                    [
                        "边界",
                        "无豁免",
                        "只豁免守卫触发的运行，不放宽为「Hash 可以随意不同」；"
                        "3 次均未触发守卫时仍要求完全一致",
                    ],
                ],
            ),
            "",
            f"- 判定函数：`full_common.guard_aware_hash_consistency`；"
            f"守卫触发检测：`one_call` 观察到同一轮对话出现多次 LLM 调用，"
            f"本运行期唯一的多调用路径是产品内置 HISTORY_GUARD 重生成。",
            f"- 全量观测缺口：{FULL_RUN_GUARD_OBSERVATION}",
            "",
            "### 11.3 S5-A/B/C 逐次结果",
            "",
        ]
    )

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
                "LLM 调用数",
                "finish",
                "prompt/completion/total",
                "citation 集合",
                "Context Hash(12)",
                "System Hash(12)",
                "Manifest(12)",
                "Manifest 关键来源",
                "历史原句",
            ],
            run_rows,
        )
    )
    lines.extend(
        [
            "",
            "- 历史原句列表示是否命中 `s5_history_use_check`（原句连续子串或三关键词）。"
            "S5-A 的预期是「未命中」（A 不注入 history）；S5-B/C 的预期是「命中」。",
            "",
            "### 11.4 逐组合判定",
            "",
        ]
    )

    verdict_rows = []
    for item in evaluations:
        history_pass = all(
            (
                (not check["pass"])
                if item["strategy"] == "A"
                else check["pass"]
            )
            for check in item["history"]
        )
        if item["strategy"] == "C":
            manifest_pass: str = (
                "PASS"
                if all(
                    detail["hasRevision"] and detail["hasEvidence"]
                    for detail in item["manifestRevisionEvidence"]
                )
                else "FAIL"
            )
        else:
            manifest_pass = "-（本策略不要求）"
        verdict_rows.append(
            [
                item["key"],
                "PASS" if item["pass"] else "FAIL",
                "PASS" if item["citation"]["pass"] else "FAIL",
                "PASS" if history_pass else "FAIL",
                manifest_pass,
                "PASS" if item["hash"]["pass"] else "FAIL",
                "是" if item["hash"]["guardedKeys"] else "否",
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
                "Hash（守卫豁免后）",
                "本组有守卫触发",
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
                    item["strategy"],
                    "命中" if check["pass"] else "未命中",
                    "是" if check["exactOriginalSentence"] else "否",
                    ",".join(
                        f"{key}={'Y' if value else 'N'}"
                        for key, value in check["keywords"].items()
                    ),
                    "PASS"
                    if (
                        (item["strategy"] == "A" and not check["pass"])
                        or (item["strategy"] != "A" and check["pass"])
                    )
                    else "FAIL",
                ]
            )
    lines.append(
        md_table(
            ["key", "策略", "判据命中", "原句连续子串", "关键词", "结果"],
            history_rows,
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
    lines.append(md_table(["key", "revision 来源", "evidence 来源", "结果"], c_rows) if c_rows else "S5-C 未执行。")
    lines.extend(["", "**Hash 逐字段**：", ""])
    hash_rows = []
    for item in evaluations:
        for annotation in item["hash"]["annotations"]:
            hash_rows.append([item["key"], annotation])
    lines.append(
        md_table(["场景-策略", "守卫豁免说明"], hash_rows) if hash_rows else "无守卫触发，无豁免。"
    )
    lines.append("")
    diagnosis: list[str] = []
    for record in runs:
        key = str(record.get("key") or "")
        citations = record.get("citationSet") or []
        if key.startswith("S5-B") and not citations:
            diagnosis.append(
                f"- **{key} 的 citation 为空**：`citationSet=[]`，产品侧 "
                "`CITATION_CHECK` 的 `total=0`。该次未触发 HISTORY_GUARD"
                f"（`historyGuardTriggered={record.get('historyGuardTriggered')}`），"
                "因此不是守卫重生成导致的记录丢失；回答使用了 memory/history，"
                "但没有给出任何可溯源的知识标签。按「引用判据（不变）」判 FAIL。"
            )
        degraded = [
            str(source.get("source_id") or "")
            for source in (manifest_bucket(record.get("manifest")).get("sources") or [])
            if str(source.get("retrieval_status") or "").upper() == "DEGRADED"
        ]
        if key.startswith("S5-C") and degraded:
            diagnosis.append(
                f"- **{key} 的 Hash 不一致来自记忆检索降级**：Manifest 中出现 "
                f"DEGRADED 来源 `{','.join(degraded)}`，"
                "并因此注入了额外的 `cognitive_status_source:memory-status:DEGRADED`，"
                "使该次的 Manifest / Context / System 指纹与另外两次不同。"
                "该次未触发 HISTORY_GUARD，按调整后判据仍属 Hash 不一致 FAIL；"
                "这不是模型随机性，而是检索依赖的运行期降级。"
            )
        answer = record.get("answerFull") or record.get("answerPreview") or ""
        check = s5_history_use_check(answer)
        if key.startswith("S5-C") and not check["pass"]:
            diagnosis.append(
                f"- **{key} 未命中历史原句**：回答引用了新版 Understanding"
                "（「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」），"
                "并声称「第一次表述的原文没有被保存下来」，但该次 Manifest 实际注入了 "
                "`history:1`、`history:2`。这是回答内容错误，不是判据误判；"
                "按「任一次回答缺少原句内容」判 FAIL。"
            )
    if diagnosis:
        lines.extend(["**失败项诊断**：", ""])
        lines.extend(diagnosis)
        lines.append("")
    if any(item["oldMarkerProblems"] for item in evaluations):
        lines.extend(
            [
                "**旧标记判据的保留记录**（不参与本批 PASS/FAIL）：",
                "",
            ]
        )
        for item in evaluations:
            for problem in item["oldMarkerProblems"]:
                lines.append(f"- {problem}")
        lines.append(
            "- 旧判据要求回答逐字包含合成标记 `【S5-HISTORY】`；"
            "该标记不是用户真实话语，本批判定改用原句内容。"
        )
        lines.append("")
    if any(item["observations"] for item in evaluations):
        lines.extend(["**运行期观测项**：", ""])
        for item in evaluations:
            for observation in item["observations"]:
                lines.append(f"- {observation}")
        lines.append("")

    lines.extend(["### 11.5 HISTORY_GUARD 触发分析", ""])
    if not guards:
        lines.append(
            "- 本批 9 次重跑均未触发 HISTORY_GUARD；无首答/重生成对照。"
        )
    else:
        for row in guards:
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
            trace = GUARD_TRACE_EVIDENCE.get(str(row["key"]))
            if trace:
                lines.append("**产品 trace 中恢复的触发明细**（来自隔离 temp 的 agent.log）：")
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
                lines.append(
                    f"- `regenerated={trace.get('regenerated')}`，"
                    f"`remainingIssues={trace.get('remainingIssues')}`，"
                    f"`regenerationError={trace.get('regenerationError')}`，"
                    f"`fallback={trace.get('fallback')}`，"
                    f"`evidenceCount={trace.get('evidenceCount')}`。"
                )
                lines.append(
                    "- 触发原因（按首答原文与 trace 对照）：首答实际写的是"
                    "「这是一次部分正确的解释，不是完全答错，也不是完全答对」，"
                    "即明确否定「完全答对」与「答错」；但产品侧 `partial_upgrade` "
                    "校验仍按字面命中这两个短语并拒绝，疑似未处理"
                    "「不是……也不是……」的否定语境。该次因此追加修复指令并重生成一次，"
                    "重生成后 `remainingIssues=[]`。"
                )
                lines.append(
                    "- 这说明守卫本次触发更像**否定语境下的疑似误判**，"
                    "而不是模型真的把「部分正确」升级成了「完全答对/答错」；"
                    "需在产品侧复核校验逻辑（见 11.10 决策点 9）。"
                )
            else:
                lines.append(f"- 触发原因（保守描述）：{_guard_reason(row)}")
            lines.append(f"- Manifest 中注入的 history：{_join(row['historyInjected'])}")
            lines.append("")
            lines.append("首次回答（HISTORY_GUARD 校验前，未推送给用户）：")
            lines.append("")
            lines.append("```text")
            lines.append((row["firstAnswer"] or "").strip())
            lines.append("```")
            lines.append("")
            lines.append("重生成回答（最终交付）：")
            lines.append("")
            lines.append("```text")
            lines.append((row["regeneratedAnswer"] or "").strip())
            lines.append("```")
            lines.append("")
        lines.append(
            "- 说明：产品侧 `HISTORY_GUARD` 的 issue 明细只写入进程内 trace，"
            "未持久化到 `agent_run`；本 harness 能确定的是「同一轮对话发生了第二次 "
            "LLM 调用，且第二次 system 指纹/长度变化」，与追加重生成指令一致。"
            "首答全文来自 harness 对 LLM 流的只读记录，仅存于隔离 temp。"
        )
    lines.append("")

    lines.extend(["### 11.6 成本记录", ""])
    guarded_calls = sum(
        1 for item in runs if item.get("historyGuardTriggered")
    )
    lines.append(
        md_table(
            ["项目", "数值"],
            [
                ["对话调用次数（记录）", len(runs)],
                ["模型实际调用次数（含守卫重生成）", len(runs) + guarded_calls],
                ["触发守卫的对话数", guarded_calls],
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
            f"- 预算核对：LLM 记录 {len(runs)} 次（上限 9），含守卫重生成 "
            f"{len(runs) + guarded_calls} 次（上限 12）；"
            f"Agent Embedding {embedding.get('calls')} 次（上限 18，按每场景 6 次计）；"
            "Tavily 0。",
            "- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，"
            "不属于每次对话的 Agent 预算。",
            "",
            "### 11.7 冻结与清理",
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
    lines.extend(["", "### 11.8 修正后 Phase 5 Step 3 结论", ""])

    if overall_pass:
        lines.append("- S5-A：PASS（citation 达标；回答未出现仅历史上下文才有的原句）。")
        lines.append("- S5-B：PASS（citation 达标；回答出现原句内容；Hash 一致性按守卫豁免规则判定）。")
        lines.append("- S5-C：PASS（citation 达标；回答出现原句内容；Manifest 含 revision 与 evidence）。")
        lines.append(
            "- Phase 5 Step 3（S5 数据重设计 + HISTORY_GUARD 判据调整后）结论为 **PASS**；"
            "仍需人工确认判据修正与 S5 数据重设计本身。"
        )
    else:
        failed = [item["key"] for item in evaluations if not item["pass"]]
        lines.append(f"- 失败组合：{failed}；Phase 5 Step 3 结论为 **FAIL**。")
        for item in evaluations:
            lines.append(
                f"- {item['key']}：{'PASS' if item['pass'] else 'FAIL'}；"
                f"{'; '.join(item['problems']) or '无失败项'}"
            )
        lines.extend(
            [
                "- S5-A 通过 citation 与反向历史判据；S5-B 的唯一失败是 "
                "S5-B-2 没有输出任何 citation（与守卫无关）；"
                "S5-C 同时存在 S5-C-2 历史原句漏引与 Hash 不一致，"
                "后者由该次记忆检索 `DEGRADED` 引入。",
                "- 因此本批结论是 **FAIL**，不是「判据设计导致的全量失败」："
                "S5-B-2 与 S5-C-2 都是可复现到具体 run 的回答/检索问题；"
                "HISTORY_GUARD 豁免规则本身在 S5-B-3 上按预期生效，"
                "但该次触发疑似是否定语境下的误判（见 11.5）。",
            ]
        )
    lines.extend(["", "### 11.9 工作区状态", ""])
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
    lines.extend(["", "### 11.10 需要人工审核的决策点", ""])
    lines.extend(
        [
            "1. **S5-A 反向判据**：本批按人工选择，S5-A 不要求出现原句，"
            "而是要求「不出现」；需确认这是最终判据口径。",
            "2. **Understanding 重设计边界**：只改了 Understanding；"
            "revision / memory 文本未改，其中仍可能出现「每次只」等词，"
            "理论上可能被 positive 关键词规则命中，需确认是否需要下一批同步收紧。",
            "3. **HISTORY_GUARD 豁免**：只豁免守卫触发的 repeat 的 "
            "contextHash / systemSha256 / systemChars / promptTokens；"
            "manifest 始终要求一致。需确认该豁免范围是否可作为最终判据。",
            "4. **守卫触发检测方式**：本 harness 用「同一轮对话出现多次 LLM 调用」"
            "判定守卫触发；产品 trace 未持久化到数据库，"
            "如后续增加其他多调用路径，检测方式需要同步调整。",
            f"5. **全量观测缺口**：{FULL_RUN_GUARD_OBSERVATION}",
            "6. **首答全文留存**：首答与重生成全文写入隔离 temp 的 "
            "`s5-rerun-results.json`，不写回业务库；需确认留存边界。",
            "7. **S5-B-2 空 citation**：该次回答没有输出任何知识标签，"
            "按调整后判据判 FAIL；是否需要在下一批单独重跑 B-2，"
            "还是把「无 citation」视为产品行为问题，需人工决定。本批预算已用满，未补跑。",
            "8. **S5-C-2 检索降级**：`memory:5` 返回 `DEGRADED`，"
            "并附带 `memory-status:DEGRADED` 来源，导致 Hash 与另外两次不同。"
            "是否把检索降级单独归类为环境失败并重跑，还是维持「未触发守卫即 FAIL」的现行口径，"
            "需人工决定。",
            "9. **HISTORY_GUARD 否定语境误判（新发现）**：S5-B-3 首答写的是"
            "「不是完全答错，也不是完全答对」，`partial_upgrade` 仍按字面命中"
            "「完全答对」「答错」并触发重生成。需产品侧确认校验是否应识别否定语境；"
            "这属于 Step 3 观测到的产品行为问题，不在本批修改边界内。",
            "",
        ]
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    if REPORT_MARKER in content:
        # 幂等：删除旧追加段后重写，避免重复堆叠。
        content = content.split(REPORT_MARKER)[0].rstrip() + "\n"
    REPORT_PATH.write_text(content + "\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "report": str(REPORT_PATH),
        "runs": len(runs),
        "combos": len(evaluations),
        "combosPass": sum(1 for item in evaluations if item["pass"]),
        "guarded": [row["key"] for row in guards],
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
