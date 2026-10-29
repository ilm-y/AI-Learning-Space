"""Phase 1 评测用例的数据模型。

用例是数据，不是代码：``docs/testing/agent-evaluation-cases.jsonl`` 每行一条，
runner 只负责解释字段。这样改一条断言不需要改执行器，reviewer 也能直接 diff。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CATEGORIES = ("A", "B", "C", "D", "E")
CATEGORY_NAMES = {
    "A": "跨会话学习与状态演化",
    "B": "证据约束与回答可靠性",
    "C": "Memory 与显式批注",
    "D": "RAG 检索与知识边界",
    "E": "教学任务与对话质量",
}
# 40 条固定配额。runner 启动时校验，防止「为了凑数复制用例」或漏写。
CATEGORY_QUOTAS = {"A": 10, "B": 8, "C": 6, "D": 8, "E": 8}

SEVERITIES = ("P0", "P1", "P2")
EXECUTION_MODES = (
    "fixture_mock",  # 进程内 Agent + fixture retriever + mock LLM
    "fixture_scripted",  # 进程内 Agent + fixture retriever + 固定模型输出
    "fixture_context",  # 只跑检索 + Context Builder，不调用模型
    "real_llm",  # fixture retriever + 真实模型（需要 agent/.env）
    "designed_only",  # 需要 MySQL / Spring Boot / SSE，Phase 1 只登记不执行
)
PROBES = (
    "chat",
    "analyze",
    "context",
    "auth_probe",
    "auto_fallback",
    "retriever_guard",
)
STATUSES = ("PASS", "FAIL", "BLOCKED", "SKIPPED", "NOT_IMPLEMENTED")
LLM_MODES = ("mock", "scripted", "real")


@dataclass(frozen=True)
class HardAssertion:
    path: str
    op: str
    value: Any
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "op": self.op, "value": self.value, "note": self.note}


@dataclass(frozen=True)
class RubricCriterion:
    """固定语义评分项。

    ``signals2`` 是「全部组都要命中」的组列表，``signals1`` 是「任一词命中」，
    ``forbidden`` 任一出现直接 0 分。这不是 LLM Judge，而是可复现的启发式：
    它只能辅助人读结果，不能替代 hard assertions。
    """

    id: str
    description: str
    score2: str
    score1: str
    score0: str
    signals2: tuple[tuple[str, ...], ...] = ()
    signals1: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    # Phase 3 的语义维度允许显式 N/A；Phase 1 的启发式 rubric 默认全部适用。
    applicable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "score2": self.score2,
            "score1": self.score1,
            "score0": self.score0,
            "signals2": [list(group) for group in self.signals2],
            "signals1": list(self.signals1),
            "forbidden": list(self.forbidden),
            "applicable": self.applicable,
        }


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    title: str
    severity: str
    execution_mode: str
    probe: str
    profile: str
    request: dict[str, Any]
    settings: dict[str, Any]
    hard_assertions: tuple[HardAssertion, ...]
    rubric: tuple[RubricCriterion, ...]
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    expected_behavior: tuple[str, ...] = ()
    forbidden_behavior: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    cleanup: str = "进程内 fixture 深拷贝；aclose 后丢弃，无持久化副作用。"
    targets_defect: str | None = None
    contract_status: str = "confirmed"
    llm: str = "mock"
    scripted_chat: str | None = None
    scripted_chat_sequence: tuple[str, ...] = ()
    scripted_analyze: str | None = None
    expect_error: dict[str, Any] | None = None
    notes: str = ""

    @property
    def category_name(self) -> str:
        return CATEGORY_NAMES[self.category]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "severity": self.severity,
            "executionMode": self.execution_mode,
            "probe": self.probe,
            "profile": self.profile,
            "request": self.request,
            "settings": self.settings,
            "preconditions": list(self.preconditions),
            "steps": list(self.steps),
            "expectedBehavior": list(self.expected_behavior),
            "forbiddenBehavior": list(self.forbidden_behavior),
            "hardAssertions": [item.as_dict() for item in self.hard_assertions],
            "semanticRubric": [item.as_dict() for item in self.rubric],
            "dependencies": list(self.dependencies),
            "cleanup": self.cleanup,
            "targetsDefect": self.targets_defect,
            "contractStatus": self.contract_status,
            "llm": self.llm,
            "scriptedChat": self.scripted_chat,
            "scriptedChatSequence": list(self.scripted_chat_sequence),
            "scriptedAnalyze": self.scripted_analyze,
            "expectError": self.expect_error,
            "notes": self.notes,
        }


def load_cases(path: Path, *, phase1_fixed_set: bool = True) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{line_no} 不是合法 JSON：{exc}") from exc
        cases.append(parse_case(item, f"{path.name}:{line_no}"))
    if phase1_fixed_set:
        validate_set(cases)
    else:
        _validate_unique_ids(cases)
    return cases


def _validate_unique_ids(cases: list[EvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"用例 id 重复：{case.id}")
        seen.add(case.id)


def parse_case(item: dict[str, Any], where: str) -> EvalCase:
    for key in (
        "id",
        "category",
        "title",
        "severity",
        "executionMode",
        "probe",
        "profile",
        "request",
        "hardAssertions",
        "semanticRubric",
    ):
        if key not in item or item[key] in (None, "", [], {}):
            raise ValueError(f"{where}: 缺少必填字段 {key}")

    category = str(item["category"])
    if category not in CATEGORIES:
        raise ValueError(f"{where}: 未知 category {category!r}")
    severity = str(item["severity"])
    if severity not in SEVERITIES:
        raise ValueError(f"{where}: 未知 severity {severity!r}")
    execution_mode = str(item["executionMode"])
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"{where}: 未知 executionMode {execution_mode!r}")
    probe = str(item["probe"])
    if probe not in PROBES:
        raise ValueError(f"{where}: 未知 probe {probe!r}")
    llm = str(item.get("llm", "mock"))
    if llm not in LLM_MODES:
        raise ValueError(f"{where}: 未知 llm {llm!r}")

    request = item["request"]
    if not isinstance(request, dict):
        raise ValueError(f"{where}: request 必须是对象")
    for key in ("userMessage", "userId", "learningSpaceId", "conversationId"):
        if key not in request:
            raise ValueError(f"{where}: request 缺少 {key}")

    assertions = tuple(
        HardAssertion(
            path=str(raw["path"]),
            op=str(raw["op"]),
            value=raw.get("value"),
            note=str(raw.get("note", "")),
        )
        for raw in item["hardAssertions"]
    )
    if not assertions:
        raise ValueError(f"{where}: hardAssertions 不能为空")

    rubric = tuple(_parse_rubric(raw, where) for raw in item["semanticRubric"])
    if not rubric:
        raise ValueError(f"{where}: semanticRubric 不能为空")

    expect_error = item.get("expectError")
    if expect_error is not None and not isinstance(expect_error, dict):
        raise ValueError(f"{where}: expectError 必须是对象")

    sequence = tuple(str(value) for value in item.get("scriptedChatSequence", []))
    if item.get("scriptedChat") and sequence:
        raise ValueError(f"{where}: scriptedChat 与 scriptedChatSequence 只能二选一")

    return EvalCase(
        id=str(item["id"]),
        category=category,
        title=str(item["title"]),
        severity=severity,
        execution_mode=execution_mode,
        probe=probe,
        profile=str(item["profile"]),
        request=request,
        settings=dict(item.get("settings") or {}),
        hard_assertions=assertions,
        rubric=rubric,
        preconditions=tuple(str(value) for value in item.get("preconditions", [])),
        steps=tuple(str(value) for value in item.get("steps", [])),
        expected_behavior=tuple(
            str(value) for value in item.get("expectedBehavior", [])
        ),
        forbidden_behavior=tuple(
            str(value) for value in item.get("forbiddenBehavior", [])
        ),
        dependencies=tuple(str(value) for value in item.get("dependencies", [])),
        cleanup=str(item.get("cleanup") or "进程内 fixture 深拷贝；aclose 后丢弃，无持久化副作用。"),
        targets_defect=item.get("targetsDefect"),
        contract_status=str(item.get("contractStatus", "confirmed")),
        llm=llm,
        scripted_chat=item.get("scriptedChat"),
        scripted_chat_sequence=sequence,
        scripted_analyze=item.get("scriptedAnalyze"),
        expect_error=expect_error,
        notes=str(item.get("notes", "")),
    )


def _parse_rubric(raw: dict[str, Any], where: str) -> RubricCriterion:
    for key in ("id", "description", "score2", "score1", "score0"):
        if not raw.get(key):
            raise ValueError(f"{where}: rubric 缺少 {key}")
    return RubricCriterion(
        id=str(raw["id"]),
        description=str(raw["description"]),
        score2=str(raw["score2"]),
        score1=str(raw["score1"]),
        score0=str(raw["score0"]),
        signals2=tuple(
            tuple(str(term) for term in group) for group in raw.get("signals2", [])
        ),
        signals1=tuple(str(term) for term in raw.get("signals1", [])),
        forbidden=tuple(str(term) for term in raw.get("forbidden", [])),
        applicable=bool(raw.get("applicable", True)),
    )


def validate_set(cases: list[EvalCase]) -> None:
    if len(cases) != 40:
        raise ValueError(f"用例总数必须是 40，实际 {len(cases)}")
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for case in cases:
        if case.id in seen:
            raise ValueError(f"用例 id 重复：{case.id}")
        seen.add(case.id)
        counts[case.category] = counts.get(case.category, 0) + 1
    for category, quota in CATEGORY_QUOTAS.items():
        if counts.get(category, 0) != quota:
            raise ValueError(
                f"类别 {category} 需要 {quota} 条，实际 {counts.get(category, 0)} 条"
            )
