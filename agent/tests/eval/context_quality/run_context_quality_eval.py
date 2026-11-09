"""D4-5 Context Quality Evaluation 命令行入口。

运行（在 agent/ 目录下）：

    python -m tests.eval.context_quality.run_context_quality_eval

输出：
- 每个 case 的 PASS / FAIL 与失败原因；
- C（当前生产链路）的聚合指标；
- A / B / C 三组基线指标表与「C 是否不劣于 A/B」；
- 已知风险探针的观察结果。

退出码：C 有 case 失败、C 在任一要求指标上劣于 A/B、或安全探针失败时为 1。
报告不落库、不调用真实 MySQL / Qdrant / LLM。
"""

from __future__ import annotations

import argparse
import json
import sys

from tests.eval.context_quality.dataset import CASES, cases_by_category
from tests.eval.context_quality.harness import (
    METRIC_KEYS,
    compare_baselines,
    run_baselines,
)
from tests.eval.context_quality.probes import run_known_risk_probes

METRIC_LABELS = {
    "relevant_context_recall": "Relevant Context Recall",
    "context_precision": "Context Precision",
    "irrelevant_context_rate": "Irrelevant Context Rate",
    "source_traceability": "Source Traceability",
    "budget_utilization": "Budget Utilization",
    "failure_visibility": "Failure Visibility",
    "personalization_accuracy": "Personalization Accuracy",
    "grounded_structure_accuracy": "Grounded Answer Accuracy",
}


def _print_case_results(results) -> None:
    print("=== Per-case results ===")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.case_id}  ({result.category})")
        if not result.passed:
            for failure in result.failures:
                print(f"    - {failure}")
    print()


def _print_aggregates(name: str, metrics: dict[str, object]) -> None:
    print(f"=== Aggregate metrics: {name} ===")
    for key in METRIC_KEYS:
        print(f"  {METRIC_LABELS[key]:30} {float(metrics[key]):.3f}")
    pass_rate = metrics.get("case_pass_rate")
    print(
        f"  {'Case pass rate':30} "
        f"{'n/a' if pass_rate is None else f'{float(pass_rate):.3f}'}"
    )
    print(f"  {'Avg context tokens':30} {float(metrics['avg_context_tokens']):.1f}")
    print(f"  {'Overflow cases':30} {int(metrics['overflow_cases'])}")
    print()


def _print_comparison(comparison: dict) -> None:
    header = (
        f"{'metric':30} {'A':>7} {'B':>7} {'C':>7} "
        f"{'better':>7} {'C<=A':>6} {'C<=B':>6}"
    )
    print("=== A / B / C baseline comparison ===")
    print(header)
    print("-" * len(header))
    for key in METRIC_KEYS:
        item = comparison[key]
        print(
            f"{key:30} {item['A']:>7.3f} {item['B']:>7.3f} {item['C']:>7.3f} "
            f"{item['criterion']:>7} "
            f"{str(item['c_no_worse_than_a']):>6} "
            f"{str(item['c_no_worse_than_b']):>6}"
        )
    print()
    print(
        "A = 仅 User Input + Knowledge（无个性化近似）；"
        "B = plan=None + 全部检索结果 + DEGRADED→EMPTY + 不剥离 Session（旧行为近似）；"
        "C = 当前 D4-2D + D4-3。"
    )
    print(
        "A/B 的指标以 C 的预期作为 gold 计算；它们不是对历史提交的逐行复现，"
        "只用于验证当前链路是否优于两个基线。"
    )
    print()


def _print_probes(probes) -> None:
    print("=== Known-risk probes ===")
    for probe in probes:
        print(f"[{'OK' if probe.ok else 'FAIL'}] {probe.name}: {probe.description}")
        for key, value in probe.observations.items():
            print(f"    {key}: {value}")
        for finding in probe.findings:
            print(f"    - {finding}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="D4-5 Context Quality Evaluation（离线、确定性）"
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        default=None,
        help="把完整结果写入指定 JSON 文件",
    )
    args = parser.parse_args(argv)

    grouped = cases_by_category()
    print("=== Dataset ===")
    print(f"cases: {len(CASES)}, categories: {len(grouped)}")
    for category, items in grouped.items():
        print(f"  {category}: {len(items)}")
    print()

    baselines = run_baselines()
    results = baselines["C"]
    comparison = compare_baselines(baselines)

    _print_case_results(results)
    _print_aggregates("C (current pipeline)", comparison["aggregates"]["C"])
    _print_aggregates("A (no personalization)", comparison["aggregates"]["A"])
    _print_aggregates("B (pre-D4-2A approximation)", comparison["aggregates"]["B"])
    _print_comparison(comparison["comparison"])

    probes = run_known_risk_probes()
    _print_probes(probes)

    failed_cases = [item for item in results if not item.passed]
    regressions = [
        key
        for key, item in comparison["comparison"].items()
        if not item["c_no_worse_than_a"] or not item["c_no_worse_than_b"]
    ]
    failed_probes = [probe.name for probe in probes if not probe.ok]
    all_passed = not failed_cases and not regressions and not failed_probes

    print("=== Summary ===")
    print(f"cases passed: {len(results) - len(failed_cases)}/{len(results)}")
    print(f"C regressions vs A/B: {regressions or 'none'}")
    print(f"failed safety probes: {failed_probes or 'none'}")
    print(f"CI status: {'PASS' if all_passed else 'FAIL'}")

    if args.json:
        report = {
            "dataset": {
                "cases": len(CASES),
                "categories": {
                    category: len(items) for category, items in grouped.items()
                },
            },
            "cases": [
                {
                    "case_id": item.case_id,
                    "category": item.category,
                    "passed": item.passed,
                    "failures": item.failures,
                    "metrics": item.metrics,
                    "checks": [
                        {
                            "category": check.category,
                            "name": check.name,
                            "passed": check.passed,
                            "detail": check.detail,
                        }
                        for check in item.checks
                    ],
                }
                for item in results
            ],
            "aggregates": comparison["aggregates"],
            "comparison": comparison["comparison"],
            "probes": [
                {
                    "name": probe.name,
                    "description": probe.description,
                    "ok": probe.ok,
                    "risk_confirmed": probe.risk_confirmed,
                    "observations": probe.observations,
                    "findings": probe.findings,
                }
                for probe in probes
            ],
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"report written to {args.json}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
