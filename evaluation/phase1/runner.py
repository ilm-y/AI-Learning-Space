"""Phase 1 执行入口与报告。

用法：

```bash
python -m evaluation.phase1 --list
python -m evaluation.phase1 --cases A01,B03,D01
python -m evaluation.phase1 --category A
python -m evaluation.phase1 --no-real
```

结果同时写 ``evaluation/phase1/reports/latest.{json,md}`` 与逐用例 artifact。
artifact 里才放完整 actual；汇总 JSON 对回答/上下文做截断，避免报告体积失控。

状态口径：

- PASS / FAIL：**执行了**，硬断言决定；
- BLOCKED：环境或真实模型不可用，不能据此判断 Agent 行为；
- SKIPPED：需要后端/数据库等本阶段未具备的条件；
- NOT_IMPLEMENTED：用例要求的能力在代码里不存在（本轮没有这种用例，
  但状态口径保留，避免未来把「没实现」误写成「测试失败」）。
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

from evaluation import CASES_DIR, REPORTS_DIR, ensure_agent_importable

from .executors import ExecOutcome, execute
from .fixtures import fixture_manifest, load_fixture
from .models import (
    CATEGORY_NAMES,
    CATEGORY_QUOTAS,
    STATUSES,
    EvalCase,
    load_cases,
)
from .recording import redact
from .scoring import RubricOutcome, run_hard_assertions, score_rubric

PHASE1_DIR = Path(__file__).resolve().parent
DEFAULT_CASES = Path("docs/testing/agent-evaluation-cases.jsonl")
DEFAULT_FIXTURE = Path("docs/testing/fixtures/phase1_two_users_two_spaces.json")
AUTO_FIXTURE = Path("docs/testing/fixtures/phase1_auto_space.json")
ARTIFACTS_DIR = PHASE1_DIR / "artifacts"
REPORT_DIR = PHASE1_DIR / "reports"


@dataclass
class CaseResult:
    case: EvalCase
    status: str
    status_reason: str
    execution_class: str
    checks: list = field(default_factory=list)
    rubric: list[RubricOutcome] = field(default_factory=list)
    actual: dict[str, Any] = field(default_factory=dict)
    llm_records: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error: dict[str, str] | None = None

    def as_dict(self, *, include_actual: bool = True) -> dict[str, Any]:
        data = {
            "id": self.case.id,
            "category": self.case.category,
            "categoryName": self.case.category_name,
            "title": self.case.title,
            "severity": self.case.severity,
            "executionMode": self.case.execution_mode,
            "executionClass": self.execution_class,
            "probe": self.case.probe,
            "profile": self.case.profile,
            "status": self.status,
            "statusReason": self.status_reason,
            "targetsDefect": self.case.targets_defect,
            "contractStatus": self.case.contract_status,
            "durationMs": self.duration_ms,
            "cleanup": self.cleanup,
            "checks": [item.as_dict() for item in self.checks],
            "rubric": [item.as_dict() for item in self.rubric],
            "llmRecords": self.llm_records,
            "error": self.error,
        }
        if include_actual:
            data["actual"] = _summarize_actual(self.actual)
        return data


async def run(
    *,
    case_ids: set[str] | None = None,
    category: str | None = None,
    modes: set[str] | None = None,
    include_real: bool = True,
    cases_path: Path = DEFAULT_CASES,
    fixture_path: Path = DEFAULT_FIXTURE,
) -> tuple[list[CaseResult], dict[str, Any]]:
    ensure_agent_importable()
    cases = load_cases(_resolve(cases_path))
    fixture_raw = load_fixture(_resolve(fixture_path))
    manifest = fixture_manifest(fixture_raw)

    selected: list[EvalCase] = []
    for case in cases:
        if case_ids and case.id not in case_ids:
            continue
        if category and case.category != category:
            continue
        if modes and case.execution_mode not in modes:
            continue
        selected.append(case)

    results: list[CaseResult] = []
    started = time.perf_counter()
    for case in selected:
        if not include_real and case.execution_mode == "real_llm":
            results.append(
                CaseResult(
                    case=case,
                    status="SKIPPED",
                    status_reason="命令行指定 --no-real，真实模型用例未执行。",
                    execution_class="REAL_LLM",
                    rubric=[],
                    cleanup={"ok": True, "mode": "not executed"},
                )
            )
            continue
        results.append(await _run_one(case, fixture_raw, _resolve(fixture_path)))

    summary = _summary(results, manifest, duration_ms=int((time.perf_counter() - started) * 1000))
    _write_reports(results, summary)
    return results, summary


async def _run_one(case: EvalCase, fixture_raw: dict, fixture_path: Path) -> CaseResult:
    started = time.perf_counter()
    outcome = await execute(case, fixture_raw=fixture_raw, fixture_path=fixture_path)
    result = CaseResult(
        case=case,
        status="FAIL",
        status_reason="",
        execution_class=_execution_class(case, outcome),
        actual=outcome.actual,
        llm_records=outcome.llm_records,
        cleanup=outcome.actual.get("cleanup", {}),
        rubric=score_rubric(case, outcome.actual),
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=outcome.error_dict,
    )

    if outcome.status_hint in ("BLOCKED", "SKIPPED", "NOT_IMPLEMENTED"):
        result.status = outcome.status_hint
        result.status_reason = outcome.status_reason or "执行器给出的前置条件未满足"
        return result

    if case.expect_error:
        result.checks = [_expected_error_check(case.expect_error, outcome.error)]
        result.status = "PASS" if all(item.passed for item in result.checks) else "FAIL"
        result.status_reason = _checks_reason(result.checks)
        result.rubric = []
        return result

    if outcome.error is not None and not outcome.actual.get("answer"):
        # 真实模型失败在执行器里已经标 BLOCKED；其余是行为失败。
        result.status = "FAIL"
        result.status_reason = f"{type(outcome.error).__name__}: {outcome.error}"
        result.rubric = []
        return result

    result.checks = run_hard_assertions(case, outcome.actual)
    if not result.checks:
        result.status = "FAIL"
        result.status_reason = "用例没有任何硬断言"
        return result
    result.status = "PASS" if all(item.passed for item in result.checks) else "FAIL"
    result.status_reason = _checks_reason(result.checks)
    return result


def _execution_class(case: EvalCase, outcome: ExecOutcome) -> str:
    if case.execution_mode == "designed_only":
        return "NOT_EXECUTED"
    if case.probe == "auto_fallback":
        return "FALLBACK"
    if case.llm == "real":
        return "REAL_LLM"
    if case.execution_mode == "fixture_context" or case.probe == "retriever_guard":
        return "FIXTURE"
    return "MOCK"


def _expected_error_check(expect: dict[str, Any], error: BaseException | None):
    from evaluation.runner.checks import CheckOutcome

    expected_type = str(expect.get("type", "Exception"))
    expected_text = expect.get("contains")
    if error is None:
        return CheckOutcome(
            path="error",
            op="raises",
            expected=f"{expected_type}({expected_text or ''})",
            actual=None,
            passed=False,
            message="期望抛出异常，但执行成功返回",
        )
    type_ok = type(error).__name__ == expected_type
    text_ok = expected_text is None or str(expected_text) in str(error)
    return CheckOutcome(
        path="error",
        op="raises",
        expected=f"{expected_type}({expected_text or ''})",
        actual=f"{type(error).__name__}: {str(error)[:500]}",
        passed=type_ok and text_ok,
        message="" if type_ok and text_ok else f"类型匹配={type_ok} 文本匹配={text_ok}",
    )


def _checks_reason(checks: list) -> str:
    failed = [item for item in checks if not item.passed]
    if not failed:
        return "全部硬断言通过"
    parts = []
    for item in failed[:6]:
        parts.append(f"{item.path} {item.op} {item.expected!r}（实际 {item.actual!r}）")
    return "未通过：" + "；".join(parts)


# ===================== 汇总与报告 =====================


def _summary(results: list[CaseResult], manifest: dict, *, duration_ms: int) -> dict[str, Any]:
    counts = {status: 0 for status in STATUSES}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_category.setdefault(
            result.case.category,
            {"total": 0, **{status: 0 for status in STATUSES}},
        )
        bucket["total"] += 1
        bucket[result.status] = bucket.get(result.status, 0) + 1
    by_class: dict[str, int] = {}
    for result in results:
        by_class[result.execution_class] = by_class.get(result.execution_class, 0) + 1
    defects = {
        result.case.targets_defect: result.case.id
        for result in results
        if result.case.targets_defect
    }
    # 统计口径（必须写死在报告里，避免「实际执行」再次对不上）：
    # - executed = 真正发起过执行的用例 = PASS + FAIL + BLOCKED
    #   （BLOCKED 已尝试执行，只是被环境/上游阻塞，不能算 PASS，但也不是未执行）
    # - completed = 得出明确行为结论的用例 = PASS + FAIL
    # - skipped / notImplemented = 设计登记但本轮未执行
    executed = sum(counts.get(status, 0) for status in ("PASS", "FAIL", "BLOCKED"))
    completed = sum(counts.get(status, 0) for status in ("PASS", "FAIL"))
    designed_only = sum(
        1 for result in results if result.case.execution_mode == "designed_only"
    )
    validation_errors: list[str] = []
    if sum(counts.values()) != len(results):
        validation_errors.append(
            f"status sum {sum(counts.values())} != total {len(results)}"
        )
    if executed + counts.get("SKIPPED", 0) + counts.get("NOT_IMPLEMENTED", 0) != len(
        results
    ):
        validation_errors.append(
            "executed + SKIPPED + NOT_IMPLEMENTED != total "
            f"({executed} + {counts.get('SKIPPED', 0)} + "
            f"{counts.get('NOT_IMPLEMENTED', 0)} != {len(results)})"
        )
    if executed != completed + counts.get("BLOCKED", 0):
        validation_errors.append(
            f"executed {executed} != completed {completed} + BLOCKED {counts.get('BLOCKED', 0)}"
        )
    skipped_designed_only = {
        result.case.id
        for result in results
        if result.status == "SKIPPED" and result.case.execution_mode == "designed_only"
    }
    if len(skipped_designed_only) != designed_only:
        validation_errors.append(
            f"designed_only {designed_only} != SKIPPED designed_only {len(skipped_designed_only)}"
        )
    return {
        "phase": "phase1",
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "python": platform.python_version(),
        "durationMs": duration_ms,
        "total": len(results),
        "counts": counts,
        "byCategory": by_category,
        "byExecutionClass": by_class,
        "defectMap": defects,
        "fixture": manifest,
        "cleanupFailures": [
            result.case.id for result in results if not result.cleanup.get("ok", True)
        ],
        "executed": executed,
        "executedDefinition": "PASS + FAIL + BLOCKED（真正发起执行；BLOCKED 不计 PASS）",
        "completed": completed,
        "completedDefinition": "PASS + FAIL（得出明确行为结论）",
        "designedOnly": designed_only,
        "realLlmCases": sum(
            1 for result in results if result.case.execution_mode == "real_llm"
        ),
        "validation": {
            "ok": not validation_errors,
            "errors": validation_errors,
        },
    }


def _write_reports(results: list[CaseResult], summary: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "runId": run_id,
        "summary": summary,
        "cases": [result.as_dict() for result in results],
    }
    (REPORT_DIR / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (REPORT_DIR / "latest.md").write_text(_markdown(results, summary, run_id), encoding="utf-8")
    artifact_dir = ARTIFACTS_DIR / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        (artifact_dir / f"{result.case.id}.json").write_text(
            json.dumps(result.as_dict(include_actual=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _markdown(results: list[CaseResult], summary: dict[str, Any], run_id: str) -> str:
    lines = [
        "# Phase 1 Agent 评测结果",
        "",
        f"- runId: `{run_id}`",
        f"- 生成时间: {summary['generatedAt']}",
        f"- Python: {summary['python']}",
        f"- 用例总数: {summary['total']}",
        "",
        "## 总览（状态口径）",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for status in STATUSES:
        lines.append(f"| {status} | {summary['counts'].get(status, 0)} |")
    lines += [
        "",
        f"实际执行（PASS+FAIL+BLOCKED）: {summary['executed']}；"
        f"得出明确结论（PASS+FAIL）: {summary['completed']}；"
        f"BLOCKED: {summary['counts'].get('BLOCKED', 0)}；"
        f"设计登记未执行（SKIPPED）：{summary['counts'].get('SKIPPED', 0)}；"
        f"真实模型用例: {summary['realLlmCases']}。",
        f"统计自检：{'通过' if summary['validation']['ok'] else '失败'}"
        + (
            "（" + "; ".join(summary["validation"]["errors"]) + "）"
            if summary["validation"]["errors"]
            else ""
        ),
        "",
        "## 分类",
        "",
        "| 类别 | 名称 | 总数 | PASS | FAIL | BLOCKED | SKIPPED |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category in ("A", "B", "C", "D", "E"):
        bucket = summary["byCategory"].get(category)
        if not bucket:
            continue
        lines.append(
            f"| {category} | {CATEGORY_NAMES[category]} | {bucket['total']} | "
            f"{bucket.get('PASS', 0)} | {bucket.get('FAIL', 0)} | "
            f"{bucket.get('BLOCKED', 0)} | {bucket.get('SKIPPED', 0)} |"
        )
    lines += [
        "",
        "## 执行类别（真实 / Mock / Fixture / Fallback）",
        "",
        "| 类别 | 数量 | 说明 |",
        "| --- | ---: | --- |",
    ]
    explanations = {
        "REAL_LLM": "fixture 数据 + 真实模型；以 RecordingLLMAdapter 的 HTTP 结果为准",
        "MOCK": "进程内 Agent + fixture 数据 + mock/固定输出模型",
        "FALLBACK": "auto 模式在无凭据时回退 Fixture",
        "NOT_EXECUTED": "designed_only，需要后端/数据库",
    }
    for key, value in summary["byExecutionClass"].items():
        lines.append(f"| {key} | {value} | {explanations.get(key, '')} |")
    lines += [
        "",
        "## 缺陷映射",
        "",
        "| 审计缺陷 | 用例 |",
        "| --- | --- |",
    ]
    for defect, case_id in summary["defectMap"].items():
        lines.append(f"| {defect} | {case_id} |")
    lines += [
        "",
        "## 清理",
        "",
        f"- 清理失败: {summary['cleanupFailures'] or '无'}",
        f"- Fixture: {summary['fixture']}",
        "",
        "## 用例明细",
        "",
        "| 用例 | 类别 | 状态 | 执行类别 | 关键失败 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        failed = [item for item in result.checks if not item.passed]
        reason = (
            "；".join(
                f"{item.path} 期望 {item.expected!r} 实际 {item.actual!r}"
                for item in failed[:2]
            )
            if failed
            else result.status_reason
        )
        if result.status not in ("FAIL",):
            reason = result.status_reason
        lines.append(
            f"| {result.case.id} | {result.case.category} | {result.status} | "
            f"{result.execution_class} | {_escape(reason)[:220]} |"
        )
    lines += [
        "",
        "## 逐用例",
        "",
    ]
    for result in results:
        lines += [
            f"### {result.case.id} {result.case.title}",
            "",
            f"- 状态：**{result.status}**（{result.status_reason}）",
            f"- 执行类别：{result.execution_class}；severity：{result.case.severity}",
            f"- 目标缺陷：{result.case.targets_defect or '无'}；合同状态：{result.case.contract_status}",
            f"- 耗时：{result.duration_ms} ms",
            f"- 清理：{result.cleanup}",
        ]
        if result.error:
            lines.append(f"- 异常：{result.error}")
        if result.checks:
            lines.append("- 硬断言：")
            for item in result.checks:
                mark = "PASS" if item.passed else "FAIL"
                lines.append(
                    f"  - [{mark}] `{item.path}` {item.op} {item.expected!r} → {item.actual!r}"
                )
        if result.rubric:
            lines.append("- 语义评分（辅助，不计入 PASS/FAIL）：")
            for item in result.rubric:
                lines.append(
                    f"  - {item.criterion_id}: {item.score}/2 —— {item.reason}"
                )
        answer = (result.actual.get("answer") or {}).get("text")
        if answer:
            lines += ["", "回答摘录：", "", "```text", redact(answer)[:1200], "```"]
        lines.append("")
    lines += [
        "## 口径说明",
        "",
        "- PASS/FAIL 只由硬断言决定；语义评分单独列出，不进入通过率。",
        "- BLOCKED 不计 PASS；SKIPPED 不计 PASS；designed_only 不计 PASS。",
        "- 本轮所有 fixture 数据均为评测专用号段，不读取真实用户数据。",
        "- 真实模型用例只保存脱敏、截断后的输出；不保存完整 context 或 Authorization。",
    ]
    return "\n".join(lines) + "\n"


def _summarize_actual(actual: dict[str, Any]) -> dict[str, Any]:
    """汇总报告里不塞完整 messages / 长上下文；完整 actual 在 artifact。"""
    summary = dict(actual)
    context = dict(summary.get("context") or {})
    if "systemInstruction" in context:
        context["systemInstruction"] = context["systemInstruction"][:6000]
    context.pop("messages", None)
    if context:
        summary["context"] = context
    answer = dict(summary.get("answer") or {})
    if "text" in answer:
        answer["text"] = redact(str(answer["text"]))[:3000]
    if answer:
        summary["answer"] = answer
    return summary


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _resolve(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 1 Agent 评测")
    parser.add_argument("--cases", help="逗号分隔的 case id，例如 A01,B03")
    parser.add_argument("--category", choices=list(CATEGORY_NAMES), help="只跑某类")
    parser.add_argument(
        "--mode",
        action="append",
        choices=[
            "fixture_mock",
            "fixture_scripted",
            "fixture_context",
            "real_llm",
            "designed_only",
        ],
        help="可重复；只跑指定执行模式",
    )
    parser.add_argument("--no-real", action="store_true", help="跳过真实模型用例")
    parser.add_argument("--list", action="store_true", help="只列用例")
    parser.add_argument("--cases-file", default=str(DEFAULT_CASES))
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_agent_importable()
    cases = load_cases(_resolve(Path(args.cases_file)))
    if args.list:
        for case in cases:
            print(
                f"{case.id}\t{case.category}\t{case.severity}\t"
                f"{case.execution_mode}\t{case.title}"
            )
        return 0
    case_ids = set(args.cases.split(",")) if args.cases else None
    results, summary = asyncio.run(
        run(
            case_ids=case_ids,
            category=args.category,
            modes=set(args.mode) if args.mode else None,
            include_real=not args.no_real,
            cases_path=Path(args.cases_file),
            fixture_path=Path(args.fixture),
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告：{REPORT_DIR / 'latest.md'}")
    failed = [result.case.id for result in results if result.status == "FAIL"]
    blocked = [result.case.id for result in results if result.status == "BLOCKED"]
    if failed:
        print("FAIL：" + ", ".join(failed))
    if blocked:
        print("BLOCKED：" + ", ".join(blocked))
    if not summary["validation"]["ok"]:
        print("统计自检失败：" + "; ".join(summary["validation"]["errors"]))
        return 2
    return 1 if failed else 0
