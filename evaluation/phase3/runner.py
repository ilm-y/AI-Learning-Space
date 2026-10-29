"""Phase 3 真实模型语义评测执行器。

用法：

```bash
python -m evaluation.phase3 --list
python -m evaluation.phase3 --run baseline
python -m evaluation.phase3 --run repeats --cases A01,B02,E01,D09 --repeat 3
python -m evaluation.phase3 --run baseline --no-judge
```

统计口径：

- **独立案例数**：用例 id 数；重复运行不增加独立案例；
- **运行次数**：每个案例每轮的一次真实调用链；
- **有效输出**：运行拿到了 >=10 个用户可见字符；
- **BLOCKED**：模型空流/上游失败，不能算语义 PASS；
- **硬断言**：程序执行，决定 PASS/FAIL/BLOCKED；
- **语义评分**：独立评审模型按用例锚点输出 0/1/2；评审失败为 JUDGE_BLOCKED，
  不生成分数，不把关键词规则冒充评分。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation import ensure_agent_importable
from evaluation.phase1.completion import (
    COMPLETE,
    EMPTY,
    ERROR,
    TRUNCATED,
    UNKNOWN,
    evaluate_completion,
)
from evaluation.phase1.executors import ExecOutcome, execute
from evaluation.phase1.fixtures import fixture_manifest, load_fixture
from evaluation.phase1.models import EvalCase, load_cases
from evaluation.phase1.scoring import run_hard_assertions

PHASE3_DIR = Path(__file__).resolve().parent
CASES_PATH = Path("docs/testing/phase3-agent-quality-cases.jsonl")
FIXTURE_PATH = Path("docs/testing/fixtures/phase1_two_users_two_spaces.json")
ARTIFACTS_DIR = PHASE3_DIR / "artifacts"
REPORTS_DIR = PHASE3_DIR / "reports"

REPEAT_CASES = ("A01", "B02", "E01", "D09")


@dataclass
class RunResult:
    case: EvalCase
    run_index: int
    hard_status: str
    hard_reason: str
    checks: list
    actual: dict[str, Any]
    llm_records: list[dict[str, Any]]
    semantics: dict[str, Any]
    completion: dict[str, Any]
    duration_ms: int
    error: dict[str, str] | None = None

    def as_dict(self, *, include_actual: bool = True) -> dict[str, Any]:
        data = {
            "caseId": self.case.id,
            "runIndex": self.run_index,
            "title": self.case.title,
            "severity": self.case.severity,
            "profile": self.case.profile,
            "hardStatus": self.hard_status,
            "hardReason": self.hard_reason,
            "completionStatus": self.completion.get("status"),
            "completion": self.completion,
            "checks": [item.as_dict() for item in self.checks],
            "semantics": self.semantics,
            "durationMs": self.duration_ms,
            "error": self.error,
            "llm": _llm_summary(self.actual.get("llm") or {}),
            "cleanup": self.actual.get("cleanup"),
        }
        if include_actual:
            data["actual"] = self.actual
        return data


def _llm_summary(llm: dict[str, Any]) -> dict[str, Any]:
    records = llm.get("records") or []
    first = records[0] if records else {}
    diagnostics = first.get("streamDiagnostics") or {}
    return {
        "classification": llm.get("classification"),
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "callCount": llm.get("callCount"),
        "outputEmitted": llm.get("outputEmitted"),
        "httpStatuses": llm.get("httpStatuses"),
        "finishReasons": llm.get("diagnosticFinishReasons"),
        "usage": diagnostics.get("usage"),
        "latencyMs": first.get("latencyMs"),
        "errorType": first.get("errorType"),
        "errorMessage": first.get("errorMessage"),
        "emptyOutputCalls": llm.get("emptyOutputCalls"),
    }


def _status_from_outcome(
    outcome: ExecOutcome, checks: list, completion: dict[str, Any]
) -> tuple[str, str]:
    if outcome.status_hint == "BLOCKED":
        return "BLOCKED", outcome.status_reason or "真实模型未产生有效输出"
    if completion.get("status") == TRUNCATED:
        # 截断是独立失败状态：无论内容看起来多正确，都不能判完整任务 PASS。
        return "FAIL", "输出被截断（finish_reason=length），任务未完成"
    if outcome.error is not None and not (
        (outcome.actual.get("answer") or {}).get("text")
    ):
        return "FAIL", f"{type(outcome.error).__name__}: {outcome.error}"
    failed = [item for item in checks if not item.passed]
    if not checks:
        return "FAIL", "没有硬断言"
    if failed:
        return (
            "FAIL",
            "未通过：" + "；".join(
                f"{item.path} {item.op} {item.expected!r}（实际 {item.actual!r}）"
                for item in failed[:6]
            ),
        )
    return "PASS", "全部硬断言通过"


async def run_case_once(
    case: EvalCase,
    *,
    fixture_raw: dict,
    fixture_path: Path,
    run_index: int,
    use_judge: bool,
) -> RunResult:
    started = time.perf_counter()
    outcome = await execute(case, fixture_raw=fixture_raw, fixture_path=fixture_path)
    checks = run_hard_assertions(case, outcome.actual)
    answer = (outcome.actual.get("answer") or {}).get("text") or ""
    completion = evaluate_completion(
        outcome.actual.get("llm") or {},
        answer,
        outcome.error_dict,
    )
    outcome.actual["completion"] = completion
    hard_status, hard_reason = _status_from_outcome(outcome, checks, completion)
    semantics: dict[str, Any]
    if hard_status == "BLOCKED" or len(str(answer)) < 10:
        semantics = {
            "status": "NO_OUTPUT",
            "dimensions": [],
            "applicableCount": 0,
            "scoredCount": 0,
            "mean": None,
            "min": None,
            "zeroDimensions": [],
            "failureTags": ["MODEL_EMPTY_OUTPUT"],
            "requiresHumanReview": True,
            "note": "没有用户可见输出，不做语义评分；不能算语义 PASS，需人工复核。",
        }
    elif not use_judge:
        semantics = {
            "status": "JUDGE_SKIPPED",
            "dimensions": [],
            "applicableCount": 0,
            "scoredCount": 0,
            "mean": None,
            "min": None,
            "zeroDimensions": [],
            "failureTags": [],
            "requiresHumanReview": True,
            "note": "命令行指定 --no-judge；语义评分待人工复核。",
        }
    else:
        from evaluation.phase3.judge import aggregate_semantics, judge_case

        judge_outcome = await judge_case(case, outcome.actual)
        semantics = aggregate_semantics(case, judge_outcome)
        if hard_status == "FAIL" and not semantics.get("zeroDimensions"):
            semantics.setdefault("failureTags", []).append("UNKNOWN")
            semantics["requiresHumanReview"] = True
        if hard_status == "PASS" and semantics.get("zeroDimensions"):
            semantics["requiresHumanReview"] = True

    semantics = _apply_completion_to_semantics(semantics, completion)

    return RunResult(
        case=case,
        run_index=run_index,
        hard_status=hard_status,
        hard_reason=hard_reason,
        checks=checks,
        actual=outcome.actual,
        llm_records=outcome.llm_records,
        semantics=semantics,
        completion=completion,
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=outcome.error_dict,
    )


def _apply_completion_to_semantics(
    semantics: dict[str, Any], completion: dict[str, Any]
) -> dict[str, Any]:
    """截断时降低任务完成度，而不是因为内容看着对就给 2。"""
    status = completion.get("status")
    tags = semantics.setdefault("failureTags", [])
    if status and status != COMPLETE and status not in tags:
        tags.append(status)
    if status == TRUNCATED:
        adjusted = False
        for item in semantics.get("dimensions") or []:
            if item.get("id") == "task_completion" and item.get("score") == 2:
                item["score"] = 1
                item["reason"] = (
                    str(item.get("reason") or "")
                    + "；输出被截断，任务完成度不能判为 2"
                ).strip("；")
                item["completionAdjusted"] = True
                adjusted = True
        semantics["completionAdjusted"] = adjusted
    if status in (TRUNCATED, EMPTY, ERROR, UNKNOWN):
        semantics["requiresHumanReview"] = True
    if status == UNKNOWN:
        semantics["completenessNote"] = (
            "provider 未提供可靠 finish_reason；完成状态为 UNKNOWN，未猜测为完整。"
        )
    return semantics


def _write_run_artifact(run_id: str, result: RunResult) -> Path:
    directory = ARTIFACTS_DIR / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.case.id}-r{result.run_index}.json"
    path.write_text(
        json.dumps(
            result.as_dict(include_actual=True), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    return path


def summarize(
    results: list[RunResult],
    *,
    run_id: str,
    mode: str,
    fixture: dict,
    duration_ms: int,
) -> dict[str, Any]:
    unique_cases = sorted({result.case.id for result in results})
    counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0}
    for result in results:
        counts[result.hard_status] = counts.get(result.hard_status, 0) + 1
    valid_outputs = sum(
        1
        for result in results
        if len(str((result.actual.get("answer") or {}).get("text") or "")) >= 10
        and result.hard_status != "BLOCKED"
    )
    dimension_distribution: dict[str, dict[str, int]] = {}
    for result in results:
        for item in result.semantics.get("dimensions") or []:
            if not item.get("applicable"):
                continue
            bucket = dimension_distribution.setdefault(
                item["id"], {"0": 0, "1": 0, "2": 0, "null": 0}
            )
            score = item.get("score")
            bucket[str(score) if score in (0, 1, 2) else "null"] += 1
    judge_blocked = sum(
        1 for result in results if result.semantics.get("status") == "JUDGE_BLOCKED"
    )
    requires_review = [result.case.id for result in results if result.semantics.get("requiresHumanReview")]
    total_llm_calls = sum(
        int((result.actual.get("llm") or {}).get("callCount") or 0)
        for result in results
    )
    finish_reasons: dict[str, int] = {}
    for result in results:
        reasons = (result.actual.get("llm") or {}).get("diagnosticFinishReasons") or []
        key = str(reasons[0]) if reasons else "unknown"
        finish_reasons[key] = finish_reasons.get(key, 0) + 1
    completion_counts: dict[str, int] = {}
    for result in results:
        status = result.completion.get("status") or UNKNOWN
        completion_counts[status] = completion_counts.get(status, 0) + 1
    truncated_runs = completion_counts.get(TRUNCATED, 0)
    return {
        "phase": "phase3",
        "runId": run_id,
        "mode": mode,
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "python": platform.python_version(),
        "durationMs": duration_ms,
        "fixture": fixture,
        "definitions": {
            "independentCases": "去重用例 id；重复运行不增加独立案例",
            "runs": "每个案例每轮的一次真实 AgentRuntime 调用链",
            "validOutputs": "回答至少 10 个用户可见字符且非 BLOCKED",
            "caseStatus": "PASS / FAIL / BLOCKED，由硬断言与截断状态决定",
            "completionStatus": "COMPLETE / TRUNCATED / EMPTY / ERROR / UNKNOWN，独立于语义得分",
            "hardAssertions": "程序执行；决定 PASS/FAIL/BLOCKED",
            "semanticScores": "独立评审模型按用例锚点输出 0/1/2；不等于人工确认",
        },
        "independentCases": len(unique_cases),
        "caseIds": unique_cases,
        "runs": len(results),
        "counts": counts,
        "caseStatusCounts": counts,
        "completionStatusCounts": completion_counts,
        "hardPassRuns": counts.get("PASS", 0),
        "validOutputs": valid_outputs,
        "blockedRuns": counts.get("BLOCKED", 0),
        "judgeBlockedRuns": judge_blocked,
        "truncatedRuns": truncated_runs,
        "incompleteRuns": sum(
            1 for result in results if result.completion.get("status") != COMPLETE
        ),
        "finishReasonDistribution": finish_reasons,
        "totalLlmCalls": total_llm_calls,
        "dimensionDistribution": dimension_distribution,
        "requiresHumanReview": requires_review,
        "results": [result.as_dict(include_actual=False) for result in results],
    }


def _repeat_consistency(results: list[RunResult]) -> dict[str, Any]:
    by_case: dict[str, list[RunResult]] = {}
    for result in results:
        by_case.setdefault(result.case.id, []).append(result)
    out: dict[str, Any] = {}
    for case_id, runs in sorted(by_case.items()):
        runs = sorted(runs, key=lambda item: item.run_index)
        statuses = [run.hard_status for run in runs]
        dims: dict[str, list[int | None]] = {}
        for run in runs:
            for item in run.semantics.get("dimensions") or []:
                if item.get("applicable"):
                    dims.setdefault(item["id"], []).append(item.get("score"))
        dim_ranges = {
            dim: {"scores": scores, "spread": max(scores) - min(scores)}
            for dim, scores in dims.items()
            if scores and None not in scores
        }
        out[case_id] = {
            "runCount": len(runs),
            "hardStatuses": statuses,
            "hardStable": len(set(statuses)) == 1,
            "dimensionRanges": dim_ranges,
            "semanticStable": all(item["spread"] <= 1 for item in dim_ranges.values())
            if dim_ranges
            else None,
            "answerCharCounts": [
                int((run.actual.get("answer") or {}).get("length") or 0)
                for run in runs
            ],
        }
    return out


def write_report(summary: dict[str, Any], results: list[RunResult]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{summary['runId']}.json"
    payload = dict(summary)
    if summary["mode"] == "repeats":
        payload["repeatConsistency"] = _repeat_consistency(results)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def run(
    *,
    run_id: str,
    case_ids: set[str] | None = None,
    repeat: int = 1,
    use_judge: bool = True,
) -> tuple[list[RunResult], dict[str, Any], Path]:
    ensure_agent_importable()
    cases = load_cases(CASES_PATH, phase1_fixed_set=False)
    fixture_raw = load_fixture(FIXTURE_PATH)
    selected = [case for case in cases if not case_ids or case.id in case_ids]
    if not selected:
        raise SystemExit(f"没有匹配的用例：{case_ids}")
    results: list[RunResult] = []
    started = time.perf_counter()
    for case in selected:
        for index in range(1, repeat + 1):
            print(
                f"[phase3] {case.id} run {index}/{repeat} ...",
                flush=True,
            )
            result = await run_case_once(
                case,
                fixture_raw=fixture_raw,
                fixture_path=FIXTURE_PATH,
                run_index=index,
                use_judge=use_judge,
            )
            artifact = _write_run_artifact(run_id, result)
            print(
                f"[phase3] {case.id} r{index}: {result.hard_status} "
                f"answer={len(str((result.actual.get('answer') or {}).get('text') or ''))} "
                f"sem={result.semantics.get('status')} -> {artifact}",
                flush=True,
            )
            results.append(result)
    mode = "repeats" if repeat > 1 else "baseline"
    summary = summarize(
        results,
        run_id=run_id,
        mode=mode,
        fixture=fixture_manifest(fixture_raw),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    path = write_report(summary, results)
    return results, summary, path


def _new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 real-LLM semantic evaluation")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--run", default="")
    parser.add_argument("--cases", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args(argv)

    ensure_agent_importable()
    cases = load_cases(CASES_PATH, phase1_fixed_set=False)
    if args.list:
        for case in cases:
            print(
                f"{case.id} | {case.severity} | {case.title} | "
                f"profile={case.profile} | maxTokens={case.settings.get('llmMaxTokens')}"
            )
        return 0

    case_ids = {item.strip() for item in args.cases.split(",") if item.strip()} or None
    run_id = args.run or _new_run_id("phase3")
    results, summary, path = asyncio.run(
        run(
            run_id=run_id,
            case_ids=case_ids,
            repeat=max(1, args.repeat),
            use_judge=not args.no_judge,
        )
    )
    print(json.dumps(summary["counts"], ensure_ascii=False))
    print(f"runs={summary['runs']} validOutputs={summary['validOutputs']} "
          f"blocked={summary['blockedRuns']} judgeBlocked={summary['judgeBlockedRuns']}")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
