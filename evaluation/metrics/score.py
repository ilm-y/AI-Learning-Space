"""把逐条用例结果汇总成可比较的指标。

第一版只报三类**能直接解释**的数：

- `pass_rate`：整条用例通过率（一条用例里所有断言都过才算过）；
- `assertion_pass_rate`：单条断言通过率 —— 用例通过率掉 10% 时，它告诉你
  是「一条用例挂了很多断言」还是「很多用例各挂一条」；
- `skipped`：环境不可用被跳过的数量（不算失败，但也不能当成通过）。

不报 precision / recall：没有独立标注的「正确答案」时，这两个数只能从断言里反推，
会制造一种虚假的严谨。当前用例的 expect 本身就是人工标注的 gold。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..runner.models import (
    CATEGORIES,
    CATEGORY_LABELS,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    CaseResult,
)


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    total = len(results)
    counts = {
        STATUS_PASSED: sum(1 for result in results if result.status == STATUS_PASSED),
        STATUS_FAILED: sum(1 for result in results if result.status == STATUS_FAILED),
        STATUS_SKIPPED: sum(1 for result in results if result.status == STATUS_SKIPPED),
    }
    executed = counts[STATUS_PASSED] + counts[STATUS_FAILED]
    checks = [check for result in results for check in result.checks]

    by_category: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        grouped[result.category].append(result)

    for category in CATEGORIES:
        items = grouped.get(category, [])
        if not items:
            continue
        category_executed = [item for item in items if item.status != STATUS_SKIPPED]
        category_checks = [check for item in items for check in item.checks]
        by_category[category] = {
            "label": CATEGORY_LABELS.get(category, category),
            "total": len(items),
            "passed": sum(1 for item in items if item.status == STATUS_PASSED),
            "failed": sum(1 for item in items if item.status == STATUS_FAILED),
            "skipped": sum(1 for item in items if item.status == STATUS_SKIPPED),
            "passRate": _rate(
                sum(1 for item in category_executed if item.status == STATUS_PASSED),
                len(category_executed),
            ),
            "assertionPassRate": _rate(
                sum(1 for check in category_checks if check.passed), len(category_checks)
            ),
        }

    by_mode: dict[str, dict[str, Any]] = {}
    for mode in ("offline", "live"):
        items = [result for result in results if result.mode == mode]
        if not items:
            continue
        executed_items = [item for item in items if item.status != STATUS_SKIPPED]
        by_mode[mode] = {
            "total": len(items),
            "passed": sum(1 for item in items if item.status == STATUS_PASSED),
            "failed": sum(1 for item in items if item.status == STATUS_FAILED),
            "skipped": sum(1 for item in items if item.status == STATUS_SKIPPED),
            "passRate": _rate(
                sum(1 for item in executed_items if item.status == STATUS_PASSED),
                len(executed_items),
            ),
        }

    return {
        "total": total,
        "passed": counts[STATUS_PASSED],
        "failed": counts[STATUS_FAILED],
        "skipped": counts[STATUS_SKIPPED],
        "passRate": _rate(counts[STATUS_PASSED], executed),
        "assertionCount": len(checks),
        "assertionPassed": sum(1 for check in checks if check.passed),
        "assertionPassRate": _rate(sum(1 for check in checks if check.passed), len(checks)),
        "byCategory": by_category,
        "byMode": by_mode,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    """没有可执行用例时返回 None，而不是 0.0 —— 「没跑」和「全挂了」不是一回事。"""
    return round(numerator / denominator, 4) if denominator else None
