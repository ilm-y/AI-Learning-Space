"""D4-5 评测执行器：执行当前 Policy + ContextBuilder，并核对预期。

执行顺序严格模拟 Runtime 在 D4-2/D4-3 下的接线方式：

1. `plan_retrieval` 产出 `RetrievalPlan`；
2. 按 Plan 的 `triggered` 决定是否调用 Fake Retriever（REQUIRED / OPTIONAL 都查，
   SKIP 不查），并复用 Runtime 的 `_apply_session_policy` 剥离不匹配的 goal/focus；
3. 把检索结果写回 `AgentState`；
4. `ContextBuilder.build` 产出上下文与 Manifest；
5. 对照 case 的预期检查检索、注入、渲染、预算。

A/B/C 三种基线都在本文件里构造，但只有 C 是当前生产链路：

- C：当前 `plan_retrieval` + `ContextBuilder`（D4-2D + D4-3）；
- B：`plan=None` 的旧 Builder 路径 + 全部检索结果。DEGRADED 折叠为 EMPTY，
  Session 不剥离。这是对 D4-2A 之前行为的**近似**，不是对旧提交的复现；
- A：只注入 User Input + Knowledge，不注入 Understanding / Memory / Evidence /
  Session / History，用于衡量「完全不做个性化」的下界。

A/B 的指标都以 C 的预期作为 gold 计算，因此它们低分只说明与当前目标的差距，
不代表旧版本当时的真实线上表现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Iterable

from app.context.builder import ContextBuilder, ContextSection
from app.context.policy import plan_retrieval
from app.context.trajectory import (
    attach_evidences,
    build_trajectories,
    collect_evidence_ids,
)
from app.retriever.base import KnowledgeQuery
from app.runtime import _apply_session_policy
from app.state.agent_state import AgentState, DetectedConcept
from app.trace import RunTrace
from tests.eval.context_quality.dataset import (
    CATALOG,
    CASES,
    Case,
    Expected,
)
from tests.eval.context_quality.fake_retriever import FakeRetriever

CONTENT_TYPES = {
    "session",
    "understanding",
    "trajectory",
    "memory",
    "knowledge",
    "evidence",
    "related_evidence",
}

DEGRADED_PATTERNS = {
    "understanding": "学习认知检索：DEGRADED",
    "memory": "长期记忆检索：DEGRADED",
    "revision": "历史轨迹检索：DEGRADED",
    "knowledge": "本轮知识检索不可用（Embedding / 向量检索失败）",
    "evidence": "原始概念证据检索：DEGRADED",
}

# 这些短语只有在**非否定语境**出现时才算错误断言。
# BASE_SYSTEM_INSTRUCTION 与 D4-2A 状态说明里大量出现「不要声称……」，不能误判。
_NEGATION_MARKERS = (
    "不要",
    "不得",
    "不能",
    "不应",
    "禁止",
    "切勿",
    "别",
    "不表示",
    "不代表",
    "不等于",
    "不说明",
    "不意味着",
    "无法确认",
    "不能据此",
    "不会",
    "并非",
)


@dataclass
class Check:
    category: str
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    category: str
    strategy: str
    checks: list[Check]
    metrics: dict[str, float]
    plan: object | None
    state: AgentState
    context: object
    retriever: FakeRetriever | None = None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[str]:
        return [
            f"{check.name}: {check.detail}"
            for check in self.checks
            if not check.passed
        ]


# =====================================================================
# Runtime 检索接线模拟
# =====================================================================


async def simulate_retrieval(
    state: AgentState, retriever: FakeRetriever, plan
) -> None:
    """按 Plan 的 triggered 调用 Fake Retriever，复刻 Runtime 的检索分支。

    只保留 D4-2 之后与上下文质量有关的分支：Knowledge / U / M / Evidence /
    Revision 的触发与复盘证据回查。并发、Resolver、规则识别、跨概念扩展不在
    D4-5 范围内，也不影响这里的预期。
    """
    concept_ids = list(plan.concepts)
    query = state.user_message or ""

    if plan.knowledge.triggered:
        result = await retriever.get_knowledge(
            KnowledgeQuery(
                user_id=state.user_id,
                space_id=state.space_id,
                question=query,
                concept_ids=tuple(concept_ids),
            )
        )
        state.knowledge = list(result.chunks)
        state.knowledge_status = result.status.value
        state.knowledge_warning = result.warning

    if plan.understanding.triggered and concept_ids:
        result = await retriever.get_understandings(
            state.user_id, state.space_id, concept_ids
        )
        state.understandings = list(result.items)
        state.understanding_status = result.status.value
        state.understanding_warning = result.warning

    if plan.memory.triggered and plan.memory_concept_ids:
        result = await retriever.get_memories(
            state.user_id,
            state.space_id,
            list(plan.memory_concept_ids),
            query,
        )
        state.memories = list(result.items)
        state.memory_status = result.status.value
        state.memory_warning = result.warning
        state.memory_detail = result.detail

    wants_evidence = plan.evidence.triggered and bool(concept_ids)
    retrieve_trajectory = plan.revision.triggered and bool(concept_ids)
    fetch_recent_evidence = wants_evidence and not retrieve_trajectory

    if fetch_recent_evidence:
        result = await retriever.get_evidences(
            state.user_id, state.space_id, concept_ids, limit=3
        )
        state.evidences = list(result.items)
        state.evidence_status = result.status.value
        state.evidence_warning = result.warning_text

    if retrieve_trajectory:
        revision_result = await retriever.get_revisions(
            state.user_id, concept_ids
        )
        state.revision_status = revision_result.status.value
        state.revision_warning = revision_result.warning
        state.trajectories = build_trajectories(
            state.understandings, revision_result.revisions
        )
        if state.trajectories:
            evidence_ids = collect_evidence_ids(
                state.trajectories, state.understandings
            )
            if evidence_ids:
                state.evidences = await retriever.get_evidence_details(
                    state.user_id, concept_ids, evidence_ids
                )
            attach_evidences(state.trajectories, state.evidences)
        elif wants_evidence:
            result = await retriever.get_evidences(
                state.user_id, state.space_id, concept_ids, limit=3
            )
            state.evidences = list(result.items)
            state.evidence_status = result.status.value
            state.evidence_warning = result.warning_text


def _apply_unrequested(state: AgentState, case: Case) -> None:
    """把「本轮 Plan 没触发、但 state 里恰好有」的数据强行放回 state。

    这是对 ContextBuilder `_apply_plan_scope` 防御层的测试，不模拟 Runtime
    的正常行为：正常 Runtime 不会在 SKIP 时发起检索。
    """
    data = case.retrieved
    for source in case.inject_unrequested:
        if source == "understanding":
            state.understandings = list(data.understandings)
            state.understanding_status = data.understanding_status
            state.understanding_warning = data.understanding_warning
        elif source == "memory":
            state.memories = list(data.memories)
            state.memory_status = data.memory_status
            state.memory_warning = data.memory_warning
            state.memory_detail = data.memory_detail
        elif source == "knowledge":
            state.knowledge = list(data.knowledge)
            state.knowledge_status = data.knowledge_status
            state.knowledge_warning = data.knowledge_warning
        elif source == "evidence":
            state.evidences = list(data.evidences)
            state.evidence_status = data.evidence_status
            state.evidence_warning = data.evidence_warning
        elif source == "trajectory":
            state.trajectories = build_trajectories(
                state.understandings, data.revisions
            )
            if state.trajectories:
                ids = collect_evidence_ids(state.trajectories, state.understandings)
                details = [item for item in data.evidence_details if item.id in ids]
                attach_evidences(state.trajectories, details)


# =====================================================================
# 状态构造
# =====================================================================


def _known_concepts() -> dict[int, object]:
    return {concept.id: concept for concept in CATALOG}


def base_state(
    case: Case,
    *,
    include_history: bool = True,
    include_session: bool = True,
) -> AgentState:
    by_id = _known_concepts()
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message=case.user_message,
        history=list(case.history) if include_history else [],
        session=case.session if include_session else None,
        detected_concepts=[
            DetectedConcept(
                concept=by_id[concept_id],
                score=1.0,
                matched_on="d4_5_fixture",
            )
            for concept_id in case.concept_ids
            if concept_id in by_id
        ],
        trace=RunTrace(run_id=1),
    )
    return state


def build_case_state(case: Case) -> tuple[AgentState, object, FakeRetriever]:
    state = base_state(case)
    plan = plan_retrieval(
        user_message=case.user_message,
        concept_ids=list(case.concept_ids),
        session=case.session,
        space_concepts=list(CATALOG),
        history=list(case.history),
    )
    state.retrieval_plan = plan
    history_review_requested = plan.review_required
    state.history_review_requested = history_review_requested
    _apply_session_policy(state, plan)

    retriever = FakeRetriever(case)
    asyncio.run(simulate_retrieval(state, retriever, plan))
    _apply_unrequested(state, case)
    return state, plan, retriever


def _builder(case: Case) -> ContextBuilder:
    return ContextBuilder(
        max_context_tokens=case.max_context_tokens,
        recent_history_limit=case.recent_history_limit,
        output_reserve_ratio=case.output_reserve_ratio,
    )


# =====================================================================
# 检查
# =====================================================================


def _manifest(context) -> dict:
    return context.context_manifest


def _entries(context) -> list[dict]:
    return list(_manifest(context).get("sources") or [])


def _entries_for(context, source_id: str) -> list[dict]:
    return [
        entry
        for entry in _entries(context)
        if entry.get("source_id") == source_id
    ]


def _statuses_for(context, source_id: str) -> set[str]:
    return {str(entry.get("injection_status")) for entry in _entries_for(context, source_id)}


def _prompt(context) -> str:
    return context.system_instruction


def _section(context, name: str) -> ContextSection | None:
    return next((item for item in context.sections if item.name == name), None)


def assertion_present(text: str, phrase: str) -> bool:
    """短语是否以「非否定」的方式出现在文本中。

    BASE 与状态说明里存在大量「不要声称 X」式禁令。直接做子串匹配会把正确的
    禁令当成错误断言，所以按同一行内短语前的否定词判断。
    """
    start = 0
    while True:
        index = text.find(phrase, start)
        if index == -1:
            return False
        line_start = text.rfind("\n", 0, index) + 1
        prefix = text[line_start:index]
        if not any(marker in prefix for marker in _NEGATION_MARKERS):
            return True
        start = index + len(phrase)


def _check_case(
    case: Case, plan, context, state: AgentState
) -> list[Check]:
    expected = case.expected
    checks: list[Check] = []
    prompt = _prompt(context)

    checks.append(
        Check(
            "intent",
            "intent",
            plan.intent.value == expected.intent,
            f"expected={expected.intent}, actual={plan.intent.value}",
        )
    )

    for key, want in expected.retrieval.items():
        decision = getattr(plan, key)
        actual = decision.need.value
        checks.append(
            Check(
                "retrieval",
                f"retrieval:{key}",
                actual == want,
                f"expected={want}, actual={actual}",
            )
        )

    if expected.session_reason is not None:
        checks.append(
            Check(
                "session",
                "session:reason",
                plan.session.reason == expected.session_reason,
                f"expected={expected.session_reason}, actual={plan.session.reason}",
            )
        )
    if expected.session_inject_goal is not None:
        checks.append(
            Check(
                "session",
                "session:inject_goal",
                plan.session.inject_goal is expected.session_inject_goal,
                f"expected={expected.session_inject_goal}, actual={plan.session.inject_goal}",
            )
        )
    if expected.session_inject_focus is not None:
        checks.append(
            Check(
                "session",
                "session:inject_focus",
                plan.session.inject_current_focus is expected.session_inject_focus,
                f"expected={expected.session_inject_focus}, "
                f"actual={plan.session.inject_current_focus}",
            )
        )
    if expected.session_concept_match is not None:
        checks.append(
            Check(
                "session",
                "session:concept_match",
                plan.session.concept_match is expected.session_concept_match,
                f"expected={expected.session_concept_match}, "
                f"actual={plan.session.concept_match}",
            )
        )

    for source_id, want in expected.injection.items():
        statuses = _statuses_for(context, source_id)
        if want == "ABSENT":
            passed = not statuses
        else:
            passed = want in statuses
        checks.append(
            Check(
                "injection",
                f"injection:{source_id}",
                passed,
                f"expected={want}, actual={sorted(statuses) or 'ABSENT'}",
            )
        )
        if want == "ABSENT":
            continue
        want_reason = expected.injection_reasons.get(source_id)
        if want_reason is None:
            if want == "SELECTED":
                want_reason = "within_budget"
            else:
                checks.append(
                    Check(
                        "injection",
                        f"reason:{source_id}",
                        False,
                        f"{want} must declare injection_reasons",
                    )
                )
                continue
        actual_reasons = {
            str(entry.get("reason")) for entry in _entries_for(context, source_id)
        }
        checks.append(
            Check(
                "injection",
                f"reason:{source_id}",
                want_reason in actual_reasons,
                f"expected={want_reason}, actual={sorted(actual_reasons) or 'ABSENT'}",
            )
        )

    for source_id in expected.must_not_inject:
        statuses = _statuses_for(context, source_id)
        checks.append(
            Check(
                "injection",
                f"must_not_inject:{source_id}",
                not statuses,
                f"unexpected statuses={sorted(statuses)}",
            )
        )

    for source_id, marker in expected.markers.items():
        want = expected.injection.get(source_id)
        present = marker in prompt
        should_present = want in {"SELECTED", "TRUNCATED"}
        checks.append(
            Check(
                "marker",
                f"marker:{source_id}",
                present is should_present,
                f"marker={'present' if present else 'absent'}, "
                f"expected={'present' if should_present else 'absent'}",
            )
        )

    for phrase in expected.required_text:
        checks.append(
            Check(
                "required_text",
                f"required:{phrase}",
                phrase in prompt,
                "missing from rendered prompt",
            )
        )

    for phrase in expected.forbidden_text:
        asserted = assertion_present(prompt, phrase)
        checks.append(
            Check(
                "forbidden",
                f"forbidden:{phrase}",
                not asserted,
                "rendered as a non-negated assertion",
            )
        )

    for name, phrases in expected.section_contains.items():
        section = _section(context, name)
        if section is None:
            checks.append(
                Check("section", f"section:{name}", False, "section not found")
            )
            continue
        for phrase in phrases:
            checks.append(
                Check(
                    "section",
                    f"section:{name}:contains:{phrase}",
                    phrase in section.content,
                    "missing from section content",
                )
            )

    for name, phrases in expected.section_excludes.items():
        section = _section(context, name)
        if section is None:
            checks.append(
                Check("section", f"section:{name}", False, "section not found")
            )
            continue
        for phrase in phrases:
            checks.append(
                Check(
                    "section",
                    f"section:{name}:excludes:{phrase}",
                    phrase not in section.content,
                    "unexpectedly present in section content",
                )
            )

    for source in expected.degraded_sources:
        pattern = DEGRADED_PATTERNS[source]
        checks.append(
            Check(
                "degraded",
                f"degraded:{source}",
                pattern in prompt,
                f"missing degraded note: {pattern}",
            )
        )
    if expected.degraded_sources:
        checks.append(
            Check(
                "degraded",
                "degraded:no_false_empty_claim",
                "结果确实为空" not in prompt,
                "rendered a definitive empty claim while a source is DEGRADED",
            )
        )

    if expected.knowledge_state is not None:
        checks.append(
            Check(
                "knowledge_state",
                "knowledge_state",
                context.knowledge_state == expected.knowledge_state,
                f"expected={expected.knowledge_state}, "
                f"actual={context.knowledge_state}",
            )
        )

    budget = _manifest(context)["budget"]
    used = int(budget["used"])
    effective = int(budget["effective"])
    checks.append(
        Check(
            "budget",
            "budget:total",
            int(budget["total"]) == case.max_context_tokens,
            f"expected={case.max_context_tokens}, actual={budget['total']}",
        )
    )
    if expected.allow_known_overflow:
        checks.append(
            Check(
                "budget",
                "budget:known_overflow_visible",
                used > effective and context.budget_exceeded,
                f"used={used}, effective={effective}, "
                f"budget_exceeded={context.budget_exceeded}",
            )
        )
        checks.append(
            Check(
                "budget",
                "budget:overrun_reason",
                bool(context.budget_overrun_reason),
                "protected overflow must carry a visible overrun reason",
            )
        )
    else:
        checks.append(
            Check(
                "budget",
                "budget:used_within_effective",
                used <= effective and not context.budget_exceeded,
                f"used={used}, effective={effective}, "
                f"budget_exceeded={context.budget_exceeded}",
            )
        )

    selected_types = {
        str(entry["type"])
        for source_id, want in expected.injection.items()
        if want in {"SELECTED", "TRUNCATED"}
        for entry in _entries_for(context, source_id)
    }
    for kind in sorted(selected_types):
        per_type = _manifest(context)["budget"]["per_type"].get(kind, {})
        checks.append(
            Check(
                "budget",
                f"budget:per_type:{kind}",
                per_type.get("injection_status") in {"SELECTED", "PARTIAL"},
                f"per_type={per_type}",
            )
        )

    for source_id in expected.personalization_sources:
        statuses = _statuses_for(context, source_id)
        injected = bool(statuses & {"SELECTED", "TRUNCATED"})
        marker = expected.markers.get(source_id)
        marker_ok = marker is None or marker in prompt
        checks.append(
            Check(
                "personalization",
                f"personalization:{source_id}",
                injected and marker_ok,
                f"statuses={sorted(statuses) or 'ABSENT'}, "
                f"marker_ok={marker_ok}",
            )
        )

    return checks


# =====================================================================
# 指标
# =====================================================================


def _is_status_source(source_id: str) -> bool:
    return "-status:" in source_id


def _selected(context, source_id: str) -> bool:
    return bool(_statuses_for(context, source_id) & {"SELECTED", "TRUNCATED"})


def _injected_content_ids(context) -> list[str]:
    return [
        str(entry["source_id"])
        for entry in _entries(context)
        if entry.get("type") in CONTENT_TYPES
        and entry.get("injection_status") in {"SELECTED", "TRUNCATED"}
    ]


def _marker_accuracy(expected: Expected, prompt: str) -> float:
    total = len(expected.markers)
    if not total:
        return 1.0
    passed = 0
    for source_id, marker in expected.markers.items():
        want = expected.injection.get(source_id)
        should_present = want in {"SELECTED", "TRUNCATED"}
        if (marker in prompt) is should_present:
            passed += 1
    return passed / total


def _failure_visibility(expected: Expected, prompt: str) -> float:
    if not expected.degraded_sources:
        return 1.0
    passed = sum(
        1
        for source in expected.degraded_sources
        if DEGRADED_PATTERNS[source] in prompt
    )
    if "结果确实为空" in prompt:
        passed = 0
    return passed / len(expected.degraded_sources)


def _personalization_accuracy(
    expected: Expected, context, prompt: str
) -> float:
    if not expected.personalization_sources:
        return 1.0
    passed = 0
    for source_id in expected.personalization_sources:
        marker = expected.markers.get(source_id)
        if _selected(context, source_id) and (marker is None or marker in prompt):
            passed += 1
    return passed / len(expected.personalization_sources)


def _structural_accuracy(expected: Expected, context, prompt: str) -> float:
    total = 0
    passed = 0
    for phrase in expected.required_text:
        total += 1
        passed += int(phrase in prompt)
    for phrase in expected.forbidden_text:
        total += 1
        passed += int(not assertion_present(prompt, phrase))
    for name, phrases in expected.section_contains.items():
        section = _section(context, name)
        for phrase in phrases:
            total += 1
            passed += int(section is not None and phrase in section.content)
    for name, phrases in expected.section_excludes.items():
        section = _section(context, name)
        for phrase in phrases:
            total += 1
            passed += int(section is not None and phrase not in section.content)
    return passed / total if total else 1.0


def compute_metrics(case: Case, context) -> dict[str, float]:
    expected = case.expected
    prompt = _prompt(context)
    budget = _manifest(context)["budget"]
    used = int(budget["used"])
    effective = max(1, int(budget["effective"]))

    gold = [
        source_id
        for source_id, status in expected.injection.items()
        if status in {"SELECTED", "TRUNCATED"} and not _is_status_source(source_id)
    ]
    injected = _injected_content_ids(context)
    injected_set = set(injected)
    gold_set = set(gold)

    matched_gold = sum(1 for source_id in gold if _selected(context, source_id))
    matched_injected = sum(1 for source_id in injected if source_id in gold_set)

    recall = matched_gold / len(gold) if gold else 1.0
    precision = matched_injected / len(injected) if injected else 1.0

    return {
        "relevant_context_recall": recall,
        "context_precision": precision,
        "irrelevant_context_rate": 1.0 - precision,
        "source_traceability": _marker_accuracy(expected, prompt),
        "budget_utilization": min(1.0, used / effective),
        "failure_visibility": _failure_visibility(expected, prompt),
        "personalization_accuracy": _personalization_accuracy(
            expected, context, prompt
        ),
        "grounded_structure_accuracy": _structural_accuracy(
            expected, context, prompt
        ),
        "context_tokens": float(used),
        "budget_overflow": 1.0 if used > effective else 0.0,
    }


# =====================================================================
# C / A / B
# =====================================================================


def run_case(case: Case) -> CaseResult:
    state, plan, retriever = build_case_state(case)
    context = _builder(case).build(state)
    checks = _check_case(case, plan, context, state)
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        strategy="C",
        checks=checks,
        metrics=compute_metrics(case, context),
        plan=plan,
        state=state,
        context=context,
        retriever=retriever,
    )


def _fold_degraded(status: str) -> str:
    """D4-2A 之前：失败/不完整被折叠成 EMPTY（这里只用于 B 的近似）。"""
    return "EMPTY" if status == "DEGRADED" else status


def run_variant(case: Case, strategy: str) -> CaseResult:
    """A / B 两个对照基线。只用于指标比较，不参与 C 的检查。"""
    if strategy == "A":
        state = base_state(case, include_history=False, include_session=False)
        data = case.retrieved
        state.knowledge = list(data.knowledge)
        state.knowledge_status = data.knowledge_status
        state.knowledge_warning = data.knowledge_warning
    elif strategy == "B":
        state = base_state(case)
        data = case.retrieved
        state.understandings = list(data.understandings)
        state.understanding_status = _fold_degraded(data.understanding_status)
        state.understanding_warning = data.understanding_warning
        state.memories = list(data.memories)
        state.memory_status = _fold_degraded(data.memory_status)
        state.memory_warning = data.memory_warning
        state.memory_detail = data.memory_detail
        state.knowledge = list(data.knowledge)
        state.knowledge_status = data.knowledge_status
        state.knowledge_warning = data.knowledge_warning
        state.evidences = list(data.evidences)
        state.evidence_status = _fold_degraded(data.evidence_status)
        state.evidence_warning = data.evidence_warning
        state.revision_status = _fold_degraded(data.revision_status)
        state.revision_warning = data.revision_warning
        state.trajectories = build_trajectories(
            state.understandings, data.revisions
        )
        if state.trajectories:
            ids = collect_evidence_ids(state.trajectories, state.understandings)
            details = [item for item in data.evidence_details if item.id in ids]
            pool = details or list(data.evidences)
            state.evidences = pool
            attach_evidences(state.trajectories, pool)
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    state.retrieval_plan = None
    context = _builder(case).build(state)
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        strategy=strategy,
        checks=[],
        metrics=compute_metrics(case, context),
        plan=None,
        state=state,
        context=context,
    )


# =====================================================================
# 聚合
# =====================================================================


METRIC_KEYS = (
    "relevant_context_recall",
    "context_precision",
    "irrelevant_context_rate",
    "source_traceability",
    "budget_utilization",
    "failure_visibility",
    "personalization_accuracy",
    "grounded_structure_accuracy",
)

HIGHER_IS_BETTER = (
    "relevant_context_recall",
    "context_precision",
    "source_traceability",
    "failure_visibility",
    "personalization_accuracy",
    "grounded_structure_accuracy",
)
LOWER_IS_BETTER = ("irrelevant_context_rate",)


def aggregate(results: Iterable[CaseResult]) -> dict[str, object]:
    items = list(results)
    if not items:
        return {}
    aggregates: dict[str, object] = {
        key: sum(item.metrics[key] for item in items) / len(items)
        for key in METRIC_KEYS
    }
    checked = [item for item in items if item.checks]
    aggregates["case_pass_rate"] = (
        sum(1 for item in checked if item.passed) / len(checked)
        if checked
        else None
    )
    aggregates["avg_context_tokens"] = (
        sum(item.metrics["context_tokens"] for item in items) / len(items)
    )
    aggregates["overflow_cases"] = sum(
        1 for item in items if item.metrics["budget_overflow"] > 0
    )
    return aggregates


def run_all_cases() -> list[CaseResult]:
    return [run_case(case) for case in CASES]


def run_baselines() -> dict[str, list[CaseResult]]:
    return {
        "A": [run_variant(case, "A") for case in CASES],
        "B": [run_variant(case, "B") for case in CASES],
        "C": run_all_cases(),
    }


def compare_baselines(baselines: dict[str, list[CaseResult]]) -> dict[str, dict]:
    aggregates = {name: aggregate(results) for name, results in baselines.items()}
    comparison: dict[str, dict] = {}
    for key in METRIC_KEYS:
        if key in HIGHER_IS_BETTER:
            criterion = "higher"
            c_ge_a = aggregates["C"][key] + 1e-9 >= aggregates["A"][key]
            c_ge_b = aggregates["C"][key] + 1e-9 >= aggregates["B"][key]
        elif key in LOWER_IS_BETTER:
            criterion = "lower"
            c_ge_a = aggregates["C"][key] <= aggregates["A"][key] + 1e-9
            c_ge_b = aggregates["C"][key] <= aggregates["B"][key] + 1e-9
        else:
            criterion = "report_only"
            c_ge_a = True
            c_ge_b = True
        comparison[key] = {
            "criterion": criterion,
            "A": aggregates["A"][key],
            "B": aggregates["B"][key],
            "C": aggregates["C"][key],
            "c_no_worse_than_a": c_ge_a,
            "c_no_worse_than_b": c_ge_b,
        }
    return {"aggregates": aggregates, "comparison": comparison}
