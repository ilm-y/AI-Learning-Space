"""用例与结果的类型定义。

用例是数据，不是代码。这样 reviewer 可以在 diff 里直接看到「这次改了哪条预期」，
而不需要在几百行 Python 里找断言。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import CheckOutcome

# 六类回归 + 一类 Agent 输出契约。前者来自 docs/analysis.md 的闭环验收点，
# 后者保证「模型乱写时不会把半份结果写进库」。
CATEGORIES = (
    "cross_session",
    "evidence_dedup",
    "memory_scope",
    "understanding_revision",
    "memory_conflict",
    "verification",
    "analysis_contract",
)

CATEGORY_LABELS = {
    "cross_session": "跨会话上下文",
    "evidence_dedup": "证据去重",
    "memory_scope": "记忆作用域",
    "understanding_revision": "理解演进",
    "memory_conflict": "记忆冲突收敛",
    "verification": "验证策略",
    "analysis_contract": "分析输出契约",
}

MODES = ("offline", "live")

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class OfflineCase:
    """进程内用例：直接调用真实的 `AgentRuntime.analyze()`。

    `engine = heuristic` 表示走 MockLLMAdapter（确定性的规则分析）；
    `engine = recorded` 表示把一段「模型原始输出」喂给解析/校验链路 ——
    测的是 sanitize 与契约，而不是模型的措辞。
    """

    id: str
    category: str
    description: str
    engine: str
    request: dict[str, Any]
    expect: dict[str, Any] = field(default_factory=dict)
    expect_error: dict[str, Any] | None = None
    llm_response: str | None = None
    fixture: str = "demo_space.json"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveCase:
    """HTTP 用例：真后端 + 真 Agent。

    当前环境没有 MySQL / 两个服务时整类标记为 skipped，而不是 failed ——
    「环境没起」和「行为回归」必须能在报告里区分开。
    """

    id: str
    category: str
    description: str
    setup: dict[str, Any]
    steps: list[dict[str, Any]]
    tags: tuple[str, ...] = ()


@dataclass
class CaseResult:
    case_id: str
    category: str
    mode: str
    status: str
    description: str = ""
    duration_ms: int = 0
    checks: list[CheckOutcome] = field(default_factory=list)
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed_checks(self) -> int:
        return sum(1 for check in self.checks if check.passed)

    @property
    def failed_checks(self) -> int:
        return sum(1 for check in self.checks if not check.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "category": self.category,
            "mode": self.mode,
            "status": self.status,
            "description": self.description,
            "durationMs": self.duration_ms,
            "checks": [check.as_dict() for check in self.checks],
            "error": self.error,
            "detail": self.detail,
        }


# ===================== 加载 =====================


def load_offline_cases(cases_dir: Path) -> list[OfflineCase]:
    """从 `cases/offline/*.jsonl` 读取。一个文件通常对应一个类别。"""
    cases: list[OfflineCase] = []
    for path in sorted((cases_dir / "offline").glob("*.jsonl")):
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_no} 不是合法 JSON：{exc}") from exc
            cases.append(_offline_case(item, f"{path.name}:{line_no}"))
    _assert_unique(cases)
    return cases


def load_live_cases(cases_dir: Path) -> list[LiveCase]:
    """从 `cases/live/*.json` 读取；文件内容可以是 `[...]` 或 `{"cases": [...]}`。"""
    cases: list[LiveCase] = []
    for path in sorted((cases_dir / "live").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("cases", []) if isinstance(raw, dict) else raw
        for index, item in enumerate(items, start=1):
            cases.append(_live_case(item, f"{path.name}#{index}"))
    _assert_unique(cases)
    return cases


def _offline_case(item: dict[str, Any], where: str) -> OfflineCase:
    _require(item, ("id", "category", "description", "request"), where)
    _validate_category(item["category"], where)
    engine = item.get("engine", "heuristic")
    if engine not in {"heuristic", "recorded"}:
        raise ValueError(f"{where}: engine 只能是 heuristic / recorded，实际 {engine!r}")
    if engine == "recorded" and not item.get("llmResponse"):
        raise ValueError(f"{where}: recorded 用例必须提供 llmResponse")
    return OfflineCase(
        id=str(item["id"]),
        category=item["category"],
        description=item["description"],
        engine=engine,
        request=item["request"],
        expect=item.get("expect", {}),
        expect_error=item.get("expectError"),
        llm_response=_llm_response(item.get("llmResponse")),
        fixture=item.get("fixture", "demo_space.json"),
        tags=tuple(item.get("tags", ())),
    )


def _llm_response(raw: Any) -> str | None:
    """`llmResponse` 既可以写成 JSON 对象（编辑 / review 更友好），也可以写成字符串
    （用于 malformed JSON、带围栏的代码块这类「模型原文」用例）。
    """
    if raw is None or isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


def _live_case(item: dict[str, Any], where: str) -> LiveCase:
    _require(item, ("id", "category", "description", "steps"), where)
    _validate_category(item["category"], where)
    return LiveCase(
        id=str(item["id"]),
        category=item["category"],
        description=item["description"],
        setup=item.get("setup", {}),
        steps=list(item["steps"]),
        tags=tuple(item.get("tags", ())),
    )


def _require(item: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    missing = [key for key in keys if not item.get(key)]
    if missing:
        raise ValueError(f"{where}: 缺少必填字段 {missing}")


def _validate_category(category: str, where: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"{where}: 未知 category {category!r}，可选：{list(CATEGORIES)}")


def _assert_unique(cases: list[Any]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"用例 id 重复：{case.id}")
        seen.add(case.id)
