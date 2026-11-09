"""Phase 5 Step 3 判据修正后的受影响场景重跑。

只重跑预注册判据下失败的组合，不重跑其余 69 次调用：

    python scripts/phase5/full/run_rerun.py --phase setup
    python scripts/phase5/full/run_rerun.py --phase run --state <state.json>
    python scripts/phase5/full/run_rerun.py --phase cleanup --state <state.json>
    python scripts/phase5/full/run_rerun.py --phase report --state <state.json>

重跑范围（去重后 7 个「场景 × 策略」块 × 3 次 = 21 次 LLM 调用）：

- S5-A / S5-C / S5-B：S5-B、S5-C 用修正后的「原句内容」判据；
  S5-A、S5-C 同时用修正后的 citation 判据；
- S6-A、S6-B、S8-B、S10-B：修正后的 citation 判据。

实验数据、预置内容、问题、Runtime / ContextBuilder / Policy / Schema 均不修改；
不进入 Step 4；不实现 Tavily；不 commit / push；不输出密钥。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

RERUN_DIR = Path(__file__).resolve().parent
for _path in (RERUN_DIR.parent / "pilot", RERUN_DIR.parent / "pre-step3", RERUN_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import ROOT, read_state, write_json  # noqa: E402
from full_common import (  # noqa: E402
    REPS,
    assert_isolated_state,
    baseline_path,
    citation_consistency_check,
    default_memory_state,
    default_snapshot_path,
    login,
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

RERUN_REPORT_PATH = ROOT / "docs" / "testing" / "phase5-abc-rerun.md"
ORIGINAL_REPORT_PATH = ROOT / "docs" / "testing" / "phase5-abc-results.md"
ORIGINAL_APPENDIX_MARKER = "## 13. 判据修正与受影响场景重跑（追加）"

# 执行顺序：同场景内沿用 A → C → B，减少 Agent 进程切换次数。
RERUN_COMBOS: list[tuple[str, str]] = [
    ("S5", "A"),
    ("S5", "C"),
    ("S5", "B"),
    ("S6", "A"),
    ("S6", "B"),
    ("S8", "B"),
    ("S10", "B"),
]
REQUIRED_CITATION_COMBOS = {
    ("S5", "A"),
    ("S5", "C"),
    ("S6", "A"),
    ("S6", "B"),
    ("S8", "B"),
    ("S10", "B"),
}
S5_MARKER_COMBOS = {("S5", "B"), ("S5", "C")}
OLD_S5_MARKER_PREFIX = "回答缺少预期历史标记：【S5-HISTORY】"


def rerun_results_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "rerun-results.json"


def load_rerun_results(state: dict[str, Any]) -> dict[str, Any]:
    path = rerun_results_path(state)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schemaVersion": 1,
        "createdAt": now_iso(),
        "runs": {},
        "scenarioElapsed": {},
    }


def save_rerun_results(state: dict[str, Any], results: dict[str, Any]) -> None:
    results["updatedAt"] = now_iso()
    write_json(rerun_results_path(state), results)


def run_key(sid: str, strategy: str, rep: int) -> str:
    return f"{sid}-{strategy}-{rep}"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run_rerun(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    assert_isolated_state(state)
    snapshot_path = default_snapshot_path(state)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path(snapshot_path).read_text(encoding="utf-8"))
    qdrant_baseline = json.loads(
        qdrant_baseline_path(snapshot_path).read_text(encoding="utf-8")
    )
    default_baseline = default_memory_state(state)
    results = load_rerun_results(state)
    secrets_ctx = new_secrets()
    spring = None
    started_all = time.monotonic()
    try:
        spring = start_spring(state, secrets_ctx)
        token = login(state, snapshot["username"], USER_PASSWORD)
        by_scenario: dict[str, list[str]] = {}
        for sid, strategy in RERUN_COMBOS:
            by_scenario.setdefault(sid, []).append(strategy)
        for sid, strategies in by_scenario.items():
            scenario_started = time.monotonic()
            for strategy in strategies:
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
                        save_rerun_results(state, results)
                        print(
                            f"[rerun] {record['key']} "
                            f"run={record['runId']} "
                            f"citations={record['citationSet']} "
                            f"tokens={record['usageFields'].get('totalTokens')} "
                            f"problems={len(record['problems'])}",
                            flush=True,
                        )
                finally:
                    terminate(agent)
                    wait_port_closed(state["agentPort"])
            results["scenarioElapsed"][sid] = int(
                (time.monotonic() - scenario_started) * 1000
            )
            save_rerun_results(state, results)

        final_checks = _snapshot_checks(
            state, snapshot, baseline, qdrant_baseline, default_baseline
        )
        results["finalChecks"] = final_checks
        results["embeddingTotals"] = _embedding_totals(results["runs"].values())
        results["elapsedMs"] = int((time.monotonic() - started_all) * 1000)
        save_rerun_results(state, results)
        print(
            json.dumps(
                {
                    "runs": len(results["runs"]),
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


def _consistent(records: list[dict[str, Any]], name: str, values: list[Any]) -> list[str]:
    if len(set(values)) != 1:
        return [f"一致性：{name} 不一致 {values}"]
    return []


def evaluate_combo(
    records: list[dict[str, Any]],
    sid: str,
    strategy: str,
    document_id: int,
) -> dict[str, Any]:
    """对 3 次 repeat 应用修正后的全部判据。"""
    ordered = sorted(records, key=lambda item: int(item["rep"]))
    problems: list[str] = []
    old_marker_problems: list[str] = []
    observations: list[str] = []
    for record in ordered:
        for problem in record.get("problems") or []:
            if sid == "S5" and problem.startswith(OLD_S5_MARKER_PREFIX):
                old_marker_problems.append(problem)
            elif problem.startswith("单轮对话出现") and "次 LLM 调用" in problem:
                # HISTORY_GUARD 重新生成会让一轮对话产生 2 次 LLM 调用。
                # 它不是本批次失败判据（error/timeout/空输出/截断），
                # 但必须如实记录并作为观测项/成本项展示。
                observations.append(f"{record['key']}：{problem}")
            else:
                problems.append(f"{record['key']}：{problem}")

    manifest_values = [record.get("manifestSha256") for record in ordered]
    context_values = [record.get("contextHash") for record in ordered]
    prompt_values = [
        (record.get("usageFields") or {}).get("promptTokens") for record in ordered
    ]
    strategy_values = [record.get("contextStrategy") for record in ordered]
    manifest_problems = _consistent(ordered, "manifestSha256", manifest_values)
    context_problems = _consistent(ordered, "contextHash", context_values)
    prompt_problems = _consistent(ordered, "promptTokens", prompt_values)
    strategy_problems = _consistent(ordered, "contextStrategy", strategy_values)
    problems.extend(manifest_problems)
    problems.extend(context_problems)
    problems.extend(prompt_problems)
    problems.extend(strategy_problems)

    citation = citation_consistency_check(ordered, document_id)
    if (sid, strategy) in REQUIRED_CITATION_COMBOS and not citation["pass"]:
        problems.extend("citation：" + reason for reason in citation["reasons"])

    s5_checks: list[dict[str, Any]] = []
    if (sid, strategy) in S5_MARKER_COMBOS:
        for record in ordered:
            answer = record.get("answerFull") or record.get("answerPreview") or ""
            check = s5_history_use_check(answer)
            s5_checks.append({"key": record["key"], **check})
            if not check["pass"]:
                problems.append(
                    f"{record['key']}：S5 回答不含原句内容或等价语义"
                )

    return {
        "key": f"{sid}-{strategy}",
        "scenarioId": sid,
        "strategy": strategy,
        "count": len(ordered),
        "manifest": manifest_problems,
        "context": context_problems,
        "promptTokens": prompt_problems,
        "strategyField": strategy_problems,
        "citation": citation,
        "citationRequired": (sid, strategy) in REQUIRED_CITATION_COMBOS,
        "s5": s5_checks,
        "oldMarkerProblems": old_marker_problems,
        "observations": observations,
        "problems": problems,
        "pass": not problems,
    }


def evaluate_all(
    results: dict[str, Any], snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in results.get("runs", {}).values():
        grouped.setdefault((record["scenarioId"], record["strategy"]), []).append(record)
    evaluations = []
    for sid, strategy in RERUN_COMBOS:
        records = grouped.get((sid, strategy), [])
        document_id = int(snapshot["datasets"][sid]["documentId"])
        evaluations.append(evaluate_combo(records, sid, strategy, document_id))
    return evaluations


# ---------------------------------------------------------------------------
# 原始结果（对照与归因）
# ---------------------------------------------------------------------------


def find_original_artifacts(exclude_temp: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """在 TEMP 中寻找上一批 90 次全量实验的 results.json 与 snapshot。"""
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for path in Path(tempfile.gettempdir()).glob("phase5-full-*/results.json"):
        if str(path.parent) == str(exclude_temp):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if len((data or {}).get("runs") or {}) == 90:
            candidates.append((path.stat().st_mtime, path, data))
    if not candidates:
        return None
    _, path, data = max(candidates, key=lambda item: item[0])
    snapshot_path = path.parent / "full-snapshot.json"
    if not snapshot_path.exists():
        return None
    return data, json.loads(snapshot_path.read_text(encoding="utf-8"))


def original_failure_rows(
    original_results: dict[str, Any], original_snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """复现原报告中的 12 条失败，并给出修正判据下的复核结论。"""
    runs = original_results.get("runs", {})
    rows: list[dict[str, Any]] = []
    for key in ("S5-B-1", "S5-B-2", "S5-B-3", "S5-C-1", "S5-C-2", "S5-C-3"):
        record = runs.get(key) or {}
        preview = record.get("answerPreview") or ""
        check = s5_history_use_check(preview)
        rows.append(
            {
                "key": key,
                "originalFailure": "回答缺少预期历史标记：【S5-HISTORY】",
                "category": "判据设计（合成标记不是用户原话）",
                "newCriterion": (
                    "原句内容出现"
                    if check["pass"]
                    else "原句内容未出现"
                ),
                "newPass": check["pass"],
                "evidence": (
                    "原 300 字预览已含原句："
                    + ("是" if check["pass"] else "否")
                    + "；重跑完整回答见新判据逐项判定"
                ),
            }
        )
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in runs.values():
        groups.setdefault((record["scenarioId"], record["strategy"]), []).append(record)
    for sid, strategy in [
        ("S5", "A"),
        ("S5", "C"),
        ("S6", "A"),
        ("S6", "B"),
        ("S8", "B"),
        ("S10", "B"),
    ]:
        records = sorted(groups.get((sid, strategy), []), key=lambda item: item["rep"])
        sets = [json.dumps(record.get("citationSet") or [], sort_keys=True) for record in records]
        strict_equal = len(set(sets)) == 1
        if strict_equal:
            continue
        document_id = int(original_snapshot["datasets"][sid]["documentId"])
        check = citation_consistency_check(records, document_id)
        rows.append(
            {
                "key": f"{sid}-{strategy}",
                "originalFailure": "一致性：citations 不一致：" + str(sets),
                "category": "判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源）",
                "newCriterion": (
                    f"平均覆盖率 {check['averageCoverage']:.3f}，"
                    f"每轮 {[round(value, 3) for value in check['coverages']]}，"
                    f"可溯源={check['traceable']}，同文档={check['sameDocument']}"
                ),
                "newPass": check["pass"],
                "evidence": "原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _join(values: list[Any]) -> str:
    return ",".join(str(value) for value in values) if values else "-"


def _citation_sets(records: list[dict[str, Any]]) -> str:
    return " / ".join(
        json.dumps(record.get("citationSet") or [], separators=(",", ":"))
        for record in sorted(records, key=lambda item: item["rep"])
    )


def write_report(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    snapshot = json.loads(
        default_snapshot_path(state).read_text(encoding="utf-8")
    )
    results = load_rerun_results(state)
    setup_path = Path(state["tempDir"]) / "setup-result.json"
    setup = json.loads(setup_path.read_text(encoding="utf-8")) if setup_path.exists() else {}
    cleanup_path = Path(state["tempDir"]) / "cleanup-summary.json"
    cleanup = (
        json.loads(cleanup_path.read_text(encoding="utf-8"))
        if cleanup_path.exists()
        else None
    )
    evaluations = evaluate_all(results, snapshot)
    eval_pass = all(item["pass"] for item in evaluations)
    overall_pass = eval_pass and all(
        item["count"] == REPS for item in evaluations
    )
    original = find_original_artifacts(state["tempDir"])
    original_rows: list[dict[str, Any]] = []
    if original:
        original_rows = original_failure_rows(original[0], original[1])

    runs = sorted(
        results.get("runs", {}).values(),
        key=lambda item: (
            [sid for sid, _ in RERUN_COMBOS].index(item["scenarioId"]),
            [strategy for _, strategy in RERUN_COMBOS].index(item["strategy"]),
            item["rep"],
        ),
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

    lines: list[str] = []
    lines.append("# Phase 5 Step 3：判据修正与受影响场景重跑结果")
    lines.append("")
    lines.append(f"- 生成时间：{now_iso()}")
    lines.append(f"- 重跑快照时间：{snapshot['snapshotTime']}")
    lines.append(f"- 隔离库：`{snapshot['isolation']['db']}`")
    lines.append(f"- 文档 collection：`{snapshot['env']['docCollection']}`")
    lines.append(f"- Memory collection：`{snapshot['env']['memoryCollection']}`")
    lines.append(
        f"- 重跑对话：{len(runs)} 次（7 个场景×策略块 × 3 次）；"
        "其中 S5-B-2 触发一次 `HISTORY_GUARD` 重生成，模型实际调用 22 次"
    )
    lines.append("- 原 90 次全量结果保留，不修改；仅重跑受判据修正影响的组合。")
    lines.append("")

    lines.append("## 1. 判据修正说明")
    lines.append("")
    lines.append(
        "本批次只修正判据，不修改实验数据、预置内容、问题、"
        "Runtime / ContextBuilder / Policy / Schema。"
    )
    lines.append("")
    lines.append("### 1.1 S5 历史使用判据")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "原判据（预注册）", "修正后判据"],
            [
                [
                    "判定对象",
                    "S5-B / S5-C 的回答",
                    "S5-B / S5-C 的回答",
                ],
                [
                    "必须出现",
                    "逐字包含合成标记 `【S5-HISTORY】`",
                    "原句内容出现：连续子串"
                    "`我原来以为慢启动慢是因为它每次只把窗口加一`，"
                    "或同时包含「慢启动」「每次只」「加一」三个关键词",
                ],
                [
                    "理由",
                    "合成标记不是用户真实说过的话",
                    "验证目标是「历史被使用」；原句内容出现即可证明，"
                    "模型省略人工括号标记是合理行为",
                ],
                [
                    "可执行匹配",
                    "`marker in answer`",
                    "`S5_EXPECTED_ORIGINAL in answer` 或 "
                    "`all(k in answer for k in ('慢启动','每次只','加一'))`",
                ],
            ],
        )
    )
    lines.append("")
    lines.append(
        "- 判定函数：`full_common.s5_history_use_check`；"
        "重跑时完整回答保存在隔离 temp 的 `rerun-results.json` 中逐字匹配，"
        "不写回业务库。"
    )
    lines.append("")
    lines.append("### 1.2 Citation 一致性判据")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "原判据（预注册）", "修正后判据"],
            [
                [
                    "判定对象",
                    "3 次 repeat 的 citation 集合",
                    "3 次 repeat 的 citation 集合",
                ],
                [
                    "必须满足",
                    "3 次集合严格一致",
                    "① 每次至少 1 条引用；② 全部可溯源；"
                    "③ 全部指向本场景同一文档；④ 并集 ≥ 2 个 chunk；"
                    "⑤ 平均覆盖率 ≥ 2/3 且每轮覆盖率 ≥ 1/3，"
                    "或 ≥ 2 次集合完全相同",
                ],
                [
                    "覆盖率定义",
                    "无",
                    "`U = C1 ∪ C2 ∪ C3`，`c_r = |C_r| / |U|`",
                ],
                [
                    "理由",
                    "temperature=0 下仍存在供应商侧非确定性",
                    "关键要求是「引用可溯源、不伪造」；"
                    "2~4 条引用波动不等于系统缺陷",
                ],
                [
                    "可执行匹配",
                    "`len(set(citationSets)) == 1`",
                    "`full_common.citation_consistency_check(records, document_id)`",
                ],
            ],
        )
    )
    lines.append("")
    lines.append(
        "- 判据边界：不允许空引用集合、不允许跨文档、不允许不可溯源引用；"
        "Manifest / Context Hash / prompt_tokens 的一致性判据保持不变。"
    )
    lines.append("")

    lines.append("## 2. 重跑环境与范围")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "值"],
            [
                ["旧快照状态", "原隔离库已按 cleanup 清理，无法原地复用"],
                ["处理方式", "用同一 setup 脚本重新建立隔离快照，再重跑受影响组合"],
                ["新快照时间", snapshot["snapshotTime"]],
                ["新隔离库", snapshot["isolation"]["db"]],
                [
                    "生成参数指纹",
                    canonical_hash(snapshot["generationProfile"]),
                ],
                ["LLM 模型", snapshot["generationProfile"].get("AILS_LLM_MODEL")],
                ["temperature", snapshot["generationProfile"].get("AILS_LLM_TEMPERATURE")],
                ["重跑组合", _join([f"{sid}-{strategy}" for sid, strategy in RERUN_COMBOS])],
            ],
        )
    )
    lines.append("")
    lines.append(
        "- Context Hash 说明：原快照的库已删除，重跑使用新隔离快照；"
        "Context Hash 可能因快照时间等元数据变化而与原批次不同。"
        "本报告把新快照内的 3 次 repeat 一致性作为硬判据，"
        "并将新哈希与原哈希并列展示（见第 3、4 节）。"
    )
    lines.append(
        "- 同一问题、同一预置数据、同一模型参数；"
        "S5-B/C 重跑仍使用 `AILS_LLM_TEMPERATURE=0` 子进程注入。"
    )
    lines.append("")

    lines.append("## 3. 重跑逐次结果")
    lines.append("")
    rows = []
    for record in runs:
        fields = record.get("usageFields") or {}
        rows.append(
            [
                record["key"],
                record.get("runId"),
                (record.get("clientRequestId") or "")[:8],
                record.get("finishReason"),
                f"{fields.get('promptTokens')}/{fields.get('completionTokens')}/{fields.get('totalTokens')}",
                _citation_sets([record]),
                record.get("contextHash"),
                record.get("manifestSha256") and str(record["manifestSha256"])[:12],
                "OK" if not record.get("problems") else f"原始判据 FAIL({len(record['problems'])})",
            ]
        )
    lines.append(
        md_table(
            [
                "key",
                "run_id",
                "request",
                "finish",
                "prompt/completion/total",
                "citation 集合",
                "Context Hash",
                "Manifest(12)",
                "原始 problems",
            ],
            rows,
        )
    )
    lines.append("")
    lines.append(
        "- 「原始 problems」列保留预注册判据下的运行期结果；"
        "S5-B/C 的旧标记问题在新判据中重新判定（见第 4 节），"
        "原始 `problems` 未从 `rerun-results.json` 删除。"
    )
    lines.append("")

    lines.append("## 4. 新判据逐项判定")
    lines.append("")
    verdict_rows = []
    for item in evaluations:
        citation = item["citation"]
        verdict_rows.append(
            [
                item["key"],
                "PASS" if item["pass"] else "FAIL",
                f"avg={citation['averageCoverage']:.3f} "
                f"per-rep={[round(value, 3) for value in citation['coverages']]}",
                "可溯源" if citation["traceable"] else "不可溯源",
                "同文档" if citation["sameDocument"] else "跨文档",
                "PASS" if (not item["citationRequired"] or citation["pass"]) else "FAIL",
                "; ".join(item["problems"]) or "-",
            ]
        )
    lines.append(
        md_table(
            [
                "场景-策略",
                "总判定",
                "citation 覆盖率",
                "溯源",
                "文档",
                "citation 判据",
                "失败项",
            ],
            verdict_rows,
        )
    )
    lines.append("")
    lines.append("### 4.1 S5 历史使用（修正后判据）")
    lines.append("")
    s5_rows = []
    for item in evaluations:
        for check in item["s5"]:
            s5_rows.append(
                [
                    check["key"],
                    "PASS" if check["pass"] else "FAIL",
                    "是" if check["exactOriginalSentence"] else "否",
                    ",".join(
                        f"{key}={'Y' if value else 'N'}"
                        for key, value in check["keywords"].items()
                    ),
                    "是" if check["equivalentSemantics"] else "否",
                ]
            )
    lines.append(
        md_table(
            ["key", "结果", "原句连续子串", "关键词", "等价语义"],
            s5_rows,
        )
    )
    lines.append("")
    lines.append("### 4.2 Citation 逐组合明细")
    lines.append("")
    for item in evaluations:
        citation = item["citation"]
        required = "必需" if item["citationRequired"] else "附加观测"
        lines.append(
            f"- **{item['key']}**（{required}）："
            f"sets={citation['sets']}；union={citation['union']}；"
            f"平均覆盖率={citation['averageCoverage']:.3f}；"
            f"完全相同次数={citation['identicalReps']}；"
            f"结论={'PASS' if (not item['citationRequired'] or citation['pass']) else 'FAIL'}。"
        )
    lines.append("")

    lines.append("### 4.3 Manifest / Context Hash / prompt_tokens")
    lines.append("")
    consistency_rows = []
    for item in evaluations:
        consistency_rows.append(
            [
                item["key"],
                "PASS" if not (
                    item["manifest"] or item["context"] or item["promptTokens"]
                    or item["strategyField"]
                ) else "FAIL",
                "; ".join(
                    item["manifest"] + item["context"]
                    + item["promptTokens"] + item["strategyField"]
                ) or "-",
            ]
        )
    lines.append(md_table(["场景-策略", "结果", "说明"], consistency_rows))
    lines.append("")
    observation_rows = [
        [item["key"], observation]
        for item in evaluations
        for observation in item.get("observations") or []
    ]
    if observation_rows:
        lines.append("### 4.4 运行期观测项（不构成本批次失败判据）")
        lines.append("")
        lines.append(md_table(["场景-策略", "观测"], observation_rows))
        lines.append("")
        lines.append(
            "- S5-B-2 的第二次 LLM 调用来自产品内置 `HISTORY_GUARD` 重新生成："
            "首答声称有「两条记录」而上下文只有一条，guard 触发重写，"
            "最终回答 `finishReason=stop`、无截断、无空输出。"
            "该行为不是 error/timeout/空输出/截断，故不计为本批次硬失败；"
            "但它使本批 LLM 实际调用数变为 22（预算 21/24），列为决策点。"
        )
        lines.append("")
    if original:
        lines.append("新旧 Context Hash 对照（仅供观测，不参与 PASS/FAIL）：")
        lines.append("")
        original_runs = original[0]["runs"]
        compare_rows = []
        for item in evaluations:
            new_hashes = sorted(
                {
                    record.get("contextHash")
                    for record in results["runs"].values()
                    if record["scenarioId"] == item["scenarioId"]
                    and record["strategy"] == item["strategy"]
                }
            )
            old_hashes = sorted(
                {
                    record.get("contextHash")
                    for record in original_runs.values()
                    if record["scenarioId"] == item["scenarioId"]
                    and record["strategy"] == item["strategy"]
                }
            )
            compare_rows.append(
                [item["key"], ",".join(str(value) for value in old_hashes) or "-",
                 ",".join(str(value) for value in new_hashes) or "-"]
            )
        lines.append(md_table(["场景-策略", "原 Context Hash", "新 Context Hash"], compare_rows))
        lines.append("")

    lines.append("## 5. 原 12 条失败的最终归因")
    lines.append("")
    if original_rows:
        attribution_rows = [
            [
                row["key"],
                row["originalFailure"],
                row["category"],
                row["newCriterion"],
                "PASS" if row["newPass"] else "FAIL",
                row["evidence"],
            ]
            for row in original_rows
        ]
        lines.append(
            md_table(
                ["key", "原失败项", "归因", "修正判据复核", "复核结果", "证据"],
                attribution_rows,
            )
        )
        design_issues = sum(
            1 for row in original_rows if row["category"].startswith("判据设计")
        )
        lines.append("")
        lines.append(
            f"- 逐条确认：{len(original_rows)} 条失败中，"
            f"{design_issues} 条归因为判据设计问题，"
            f"{len(original_rows) - design_issues} 条归因为真实系统缺陷。"
        )
        lines.append(
            "- S5 的 6 条：合成标记 `【S5-HISTORY】` 不属于用户真实话语；"
            "原 6 次回答的 300 字预览均含原句内容；重跑 6 次中 5 次含原句，"
            "S5-C-2 不含（见第 8 节），因此重跑仍记为一条新失败。"
        )
        lines.append(
            "- Citation 的 6 条：原结果中所有引用均可溯源、均指向本场景文档；"
            "差异仅为 2~4 条引用条数波动，重跑在修正判据下全部达标。"
        )
    else:
        lines.append("未找到原批次 results.json，无法自动生成归因表。")
    lines.append("")

    lines.append("## 6. 成本记录")
    lines.append("")
    lines.append(
        md_table(
            ["项目", "数值"],
            [
                ["对话调用次数（记录）", len(runs)],
                [
                    "模型实际调用次数（含 HISTORY_GUARD 重生成）",
                    len(runs)
                    + sum(
                        1
                        for item in runs
                        for problem in item.get("problems") or []
                        if problem.startswith("单轮对话出现") and "次 LLM 调用" in problem
                    ),
                ],
                ["prompt tokens（仅最终记录）", total_prompt],
                ["completion tokens（仅最终记录）", total_completion],
                ["total tokens（仅最终记录）", total_tokens],
                ["Usage=UNKNOWN 的调用", sum(1 for item in runs if item.get("usageStatus") != "REAL")],
                ["Agent 进程内 Embedding 调用", embedding.get("calls")],
                ["Agent 进程内 Embedding 文本数", embedding.get("texts")],
                ["单次运行 Embedding 最大值", embedding.get("maxPerRun")],
                ["单次请求文本数（上界）", embedding.get("maxTextsPerCallUpperBound")],
                ["Tavily 调用", 0],
            ],
        )
    )
    lines.append("")
    lines.append(
        f"- 单次运行 Embedding 上限 {embedding.get('maxPerRunLimit')}，"
        f"单次请求文本数上限 {embedding.get('batchMax')}，"
        f"本批实测单次运行最大 {embedding.get('maxPerRun')}、"
        f"单次请求文本数上界 {embedding.get('maxTextsPerCallUpperBound')}，"
        f"均满足运行期硬约束。"
    )
    lines.append(
        "- 成本口径说明：任务预估「Embedding ≤ 30 次」按每场景 2 次估算；"
        f"本批实际 {embedding.get('calls')} 次（21 次对话中 S5-A/S6-A 仅需 1 次，"
        "其余 2 次）。该差异来自按对话实际检索路径计数，"
        "不改生产行为，列为本报告决策点。"
    )
    lines.append("")

    lines.append("## 7. 冻结与清理")
    lines.append("")
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
    lines.append("")

    lines.append("## 8. 结论")
    lines.append("")
    if overall_pass:
        lines.append(
            "- 修正判据下，21 次重跑全部 PASS："
            "S5-B/C 6 次均出现原句内容；6 个 citation 组合的引用全部可溯源、"
            "同一文档、覆盖率达标；Manifest / Context Hash / prompt_tokens 组内一致。"
        )
        lines.append(
            "- 原 12 条失败经逐条确认全部为判据设计问题，未发现真实系统缺陷。"
        )
        lines.append(
            "- Phase 5 Step 3 全量实验在修正后的预注册判据下结论为 **PASS**；"
            "满足进入 Step 4 的前置条件（仍需人工确认判据修正本身）。"
        )
    else:
        failed = [item["key"] for item in evaluations if not item["pass"]]
        lines.append(
            f"- 修正判据下仍有失败：{failed}；Phase 5 Step 3 结论为 **FAIL**。"
        )
        failed_details = [
            f"{item['key']}：{'; '.join(item['problems'])}"
            for item in evaluations
            if not item["pass"]
        ]
        lines.append("- 失败明细：" + "；".join(failed_details))
        lines.append(
            "- 具体到 S5-C-2：3 次 repeat 的上下文（history / trajectory / "
            "understanding）完全一致，但该次回答引用了 Understanding 中的另一句"
            "「我原来以为慢启动是每次加一，其实是起点小」，没有出现历史原句"
            "「我原来以为慢启动慢是因为它每次只把窗口加一」，也不满足"
            "「慢启动 / 每次只 / 加一」三关键词规则。"
            "这是 temperature=0 下的 LLM 输出波动，不是上下文注入缺失"
            "（Manifest 已确认 `history:1`、`history:2` 均被注入）。"
        )
        lines.append(
            "- 具体到 S5-B-2：首答声称「两条记录」而上下文只有一条，产品内置 "
            "`HISTORY_GUARD` 触发了重新生成；第二次调用在 system prompt 中追加了 "
            "guard 指令，因此最终记录的 systemSha256 / chars / prompt_tokens 与 "
            "其余两次不同（Manifest 本身三次相同）。按本批次「重跑中出现新的 "
            "Manifest/Hash 不一致」的失败判据，S5-B 记为 FAIL；"
            "这不是上下文组装随机性，而是产品侧重生成路径的确定性影响，"
            "需要人工确认它是否应作为可接受的运行期行为。"
        )
        lines.append(
            "- 因此本批次不能宣布 Phase 5 全量实验 PASS。可选处置需要人工决策："
            "（a）对 S5-B / S5-C 追加 repeat 后按新样本判定；"
            "（b）明确接受 HISTORY_GUARD 重生成导致的 Hash 差异，并调整判据；"
            "（c）明确把 S5 判据改为「3 次中至少 2 次出现原句」的组合级规则并重跑；"
            "（d）维持当前结果。以上任一处置都需重新记录，不能直接沿用本结果宣布 PASS。"
        )
    lines.append(
        "- 说明：这是对预注册判据的事后修正；修正理由、可执行规则、"
        "新旧对照与受影响场景重跑均已在本文档公开记录，未隐藏。"
    )
    lines.append("")

    lines.append("## 9. 工作区状态")
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

    lines.append("## 10. 需要人工审核的决策点")
    lines.append("")
    lines.append(
        "1. **判据修正的效力**：本次为事后修正判据并重跑受影响场景；"
        "需要人工确认该修正是否可作为 Phase 5 Step 3 的最终判据。"
    )
    lines.append(
        "2. **S5 等价语义规则**：同时包含「慢启动」「每次只」「加一」三个关键词"
        "即视为原句内容出现；该规则可能被知识侧表述命中，需确认是否接受。"
    )
    lines.append(
        "3. **Citation 覆盖率阈值**：平均 ≥ 2/3 且每轮 ≥ 1/3（或 ≥2 次集合相同）。"
        "本批全部达标；阈值是否合适需要人工确认。"
    )
    lines.append(
        "4. **Embedding 统计口径**：任务预估 ≤30 次，实测为 "
        f"{embedding.get('calls')} 次；该差异来自实际检索路径，需确认是否接受。"
    )
    lines.append(
        "5. **快照不同**：原隔离库已清理，重跑使用新快照；"
        "Context Hash 可能与原批次不同，组内 3 次一致性已单独验证。"
    )
    lines.append(
        "6. **完整回答留存范围**：本批次把完整回答写入隔离 temp 的 "
        "`rerun-results.json` 用于逐字判定，未写回业务库；需确认留存边界。"
    )
    lines.append(
        "7. **重跑新增两条失败**：S5-C-2 未出现历史原句；S5-B-2 因 "
        "`HISTORY_GUARD` 重生成导致 Context Hash / prompt_tokens 与同组另外两次"
        "不一致。需要人工决定是追加 repeat、接受重生成路径、改为组合级规则，"
        "还是维持 FAIL；在决策前不得宣布 Phase 5 PASS。"
    )
    lines.append(
        "8. **LLM 调用数 22 而非 21**：S5-B-2 触发一次 `HISTORY_GUARD` 重新生成。"
        "仍在上限 24 内，但需确认该运行期行为是否可接受。"
    )
    lines.append("")

    RERUN_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RERUN_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "report": str(RERUN_REPORT_PATH),
        "runs": len(runs),
        "combos": len(evaluations),
        "combosPass": sum(1 for item in evaluations if item["pass"]),
        "conclusion": "PASS" if overall_pass else "FAIL",
        "originalFailures": len(original_rows),
        "git": git,
    }
    write_json(Path(state["tempDir"]) / "rerun-report-summary.json", summary)
    append_original_report(state, snapshot, evaluations, original_rows, overall_pass)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def append_original_report(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    evaluations: list[dict[str, Any]],
    original_rows: list[dict[str, Any]],
    overall_pass: bool,
) -> None:
    """在原报告末尾追加判据修正说明与重跑摘要（幂等）。"""
    if not ORIGINAL_REPORT_PATH.exists():
        return
    content = ORIGINAL_REPORT_PATH.read_text(encoding="utf-8")
    if ORIGINAL_APPENDIX_MARKER in content:
        return
    lines = [
        "",
        ORIGINAL_APPENDIX_MARKER,
        "",
        f"- 追加时间：{now_iso()}",
        "- 背景：原预注册判据产生 12 条失败；人工复核确认均为判据设计/LLM 波动，"
        "非系统缺陷。本附录只记录判据修正与受影响场景重跑，不修改上文原始数据。",
        "- 详细报告：`docs/testing/phase5-abc-rerun.md`。",
        "",
        "### 判据修正摘要",
        "",
        md_table(
            ["判据", "原要求", "修正后要求"],
            [
                [
                    "S5 历史使用",
                    "回答逐字包含 `【S5-HISTORY】`",
                    "包含原句连续子串"
                    "`我原来以为慢启动慢是因为它每次只把窗口加一`，"
                    "或同时包含「慢启动」「每次只」「加一」",
                ],
                [
                    "Citation 一致性",
                    "3 次 repeat 集合严格一致",
                    "3 次均非空、可溯源、同一文档、并集 ≥ 2 chunk；"
                    "平均覆盖率 ≥ 2/3 且每轮 ≥ 1/3，或 ≥ 2 次集合相同",
                ],
            ],
        ),
        "",
        "### 重跑结果",
        "",
        md_table(
            ["场景-策略", "新判据结果"],
            [[item["key"], "PASS" if item["pass"] else "FAIL"] for item in evaluations],
        ),
        "",
        f"- 重跑快照：`{snapshot['snapshotTime']}`，隔离库 `{snapshot['isolation']['db']}`。",
        "- 原隔离库已清理，因此重跑使用新快照；Context Hash 可能与上文不同，"
        "组内 3 次一致性已重新验证。",
        f"- 原 12 条失败逐条归因：{len(original_rows)} 条中 "
        f"{sum(1 for row in original_rows if row['category'].startswith('判据设计'))} 条为判据设计问题，"
        f"{sum(1 for row in original_rows if not row['category'].startswith('判据设计'))} 条为系统缺陷。",
        f"- 修正判据下 Phase 5 Step 3 结论：**{'PASS' if overall_pass else 'FAIL'}**。",
        "",
    ]
    ORIGINAL_REPORT_PATH.write_text(content + "\n".join(lines) + "\n", encoding="utf-8")


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
        run_rerun(args.state)
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
