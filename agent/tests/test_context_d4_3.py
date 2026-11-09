"""D4-3：ContextBuilder 按类型排序 / 预算 / 压缩 / Manifest 的契约测试。

这些测试直接构造 `AgentState + RetrievalPlan`，不连后端、不连 Qdrant。
它们只钉住 D4-3 新增的边界：

1. 历史 Evidence 再长、再「相关」也不能改写或挤掉当前 Understanding；
2. 类型额度触发分层压缩，而不是整类静默消失；
3. REQUIRED 先于 OPTIONAL 分配；
4. 同一 source_id 只注入一次；
5. 语义冗余降低细节但保留两边（Understanding 完整 + Memory/Evidence 摘要）；
6. 冲突两边都保留，并带来源与时间；
7. Manifest 只有 source_id / 状态 / 原因 / token，不含正文、prompt、query；
8. D4-2A 的 DEGRADED / NOT_QUERIED 措辞不被 D4-3 改坏；
9. Revision 只在 LEARNING_REVIEW 注入。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from app.context.builder import ContextBuilder
from app.context.policy import (
    RetrievalNeed,
    SourceDecision,
    plan_retrieval,
)
from app.schemas import (
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceType,
    LearningTrajectory,
    MemoryItem,
    MemoryScopeType,
    MemoryType,
    Misconception,
    MisconceptionStatus,
    Understanding,
    UnderstandingRevision,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace

CONCEPT_ID = 10
CONCEPT = Concept(id=CONCEPT_ID, canonical_name="TLB", display_name="TLB（快表）")

T0 = datetime(2026, 1, 1, 9, 0)
T1 = datetime(2026, 1, 2, 9, 0)

EVIDENCE_QUESTION = "为什么 TLB 的当前理解是这样？依据是什么"
PLAIN_QUESTION = "TLB 是什么？"
REVIEW_QUESTION = "我之前对 TLB 的理解错在哪里？"


def make_state(message: str, **overrides) -> AgentState:
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message=message,
        trace=RunTrace(run_id=1),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def plan_for(message: str):
    return plan_retrieval(
        user_message=message,
        concept_ids=[CONCEPT_ID],
        session=None,
        space_concepts=[CONCEPT],
        history=[],
    )


def understanding(
    *,
    summary: str = "CURRENT-UNDERSTANDING-MARKER 用户理解 TLB 是页表项的缓存。",
    parts: list[str] | None = None,
    updated_at: datetime | None = T1,
) -> Understanding:
    return Understanding(
        id=500,
        concept_id=CONCEPT_ID,
        concept_name="TLB",
        summary=summary,
        known_parts=parts
        or ["TLB 是页表项的缓存", "TLB Hit 与 TLB Miss 的区别"],
        misconceptions=[
            Misconception(
                text="把 TLB Miss 与 Page Fault 混为一谈",
                status=MisconceptionStatus.ACTIVE,
            )
        ],
        status=UnderstandingStatus.MISCONCEPTION,
        updated_at=updated_at,
    )


def memory(
    memory_id: int,
    content: str,
    *,
    scope: MemoryScopeType = MemoryScopeType.CONCEPT,
    memory_type: MemoryType = MemoryType.LEARNING,
    importance: float | None = None,
    created_at: datetime | None = T0,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        scope_type=scope,
        learning_space_id=1,
        concept_id=CONCEPT_ID if scope is MemoryScopeType.CONCEPT else None,
        memory_type=memory_type,
        content=content,
        importance=importance,
        created_at=created_at,
        updated_at=created_at,
    )


def evidence(
    evidence_id: int,
    observation: str,
    *,
    created_at: datetime | None = T1,
    evidence_type: EvidenceType = EvidenceType.OBSERVATION,
    correctness: EvidenceCorrectness = EvidenceCorrectness.UNKNOWN,
    cross: bool = False,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        concept_id=CONCEPT_ID,
        evidence_type=evidence_type,
        observation=observation,
        correctness=correctness,
        created_at=created_at,
        is_cross_concept=cross,
        source_concept_id=2 if cross else None,
        related_via="RELATED" if cross else None,
    )


def section(context, name: str):
    return next(item for item in context.sections if item.name == name)


def system(context) -> str:
    return context.messages[0].content


# ===================== 1. 类型排序：历史证据不能挤掉当前 Understanding =====================


def test_historical_evidence_cannot_override_current_understanding() -> None:
    long_evidence = "HIGH-SIMILARITY-EVIDENCE " + "旧证据正文" * 300 + " EVIDENCE-TAIL"
    state = make_state(
        EVIDENCE_QUESTION,
        understandings=[understanding()],
        evidences=[evidence(11, long_evidence)],
    )
    state.retrieval_plan = plan_for(EVIDENCE_QUESTION)

    context = ContextBuilder(
        max_context_tokens=1200, output_reserve_ratio=0.0
    ).build(state)

    current = section(context, "understanding:10")
    past = section(context, "evidence:11")
    assert current.dropped is False
    assert current.tier == "full"
    assert "CURRENT-UNDERSTANDING-MARKER" in current.content
    # Evidence 只允许被压缩，不允许把当前快照顶掉。
    assert past.dropped is False
    assert past.tier != "full"
    assert "EVIDENCE-TAIL" not in system(context)
    assert "CURRENT-UNDERSTANDING-MARKER" in system(context)


# ===================== 2. 类型额度：分层压缩，不整类丢弃 =====================


def test_type_budget_compresses_instead_of_dropping_whole_category() -> None:
    memories = [
        memory(400 + index, f"MEMORY-{index}-MARKER " + "长期记忆内容" * 120)
        for index in range(3)
    ]
    state = make_state(PLAIN_QUESTION, memories=memories)
    state.retrieval_plan = plan_for(PLAIN_QUESTION)

    context = ContextBuilder(
        max_context_tokens=600, output_reserve_ratio=0.0
    ).build(state)
    memory_sections = [
        item for item in context.sections if item.name.startswith("memory:")
    ]

    kept = [item for item in memory_sections if not item.dropped]
    assert kept, "类型额度不能把整类 Memory 静默丢掉"
    assert any(item.tier in {"structured", "one_line"} for item in kept)
    per_type = context.context_manifest["budget"]["per_type"]["memory"]
    assert per_type["injection_status"] == "PARTIAL"


# ===================== 3. REQUIRED 优先于 OPTIONAL =====================


def test_required_type_is_allocated_before_optional() -> None:
    base = plan_for(PLAIN_QUESTION)
    state = make_state(
        PLAIN_QUESTION,
        understandings=[understanding(parts=["已理解" + "内容" * 80])],
        memories=[memory(410, "OPTIONAL-MEMORY-MARKER " + "记忆" * 300)],
    )
    state.retrieval_plan = replace(
        base,
        memory=SourceDecision(RetrievalNeed.OPTIONAL, "test_optional_memory"),
        knowledge=SourceDecision(RetrievalNeed.SKIP, "test_skip_knowledge"),
    )

    context = ContextBuilder(
        max_context_tokens=500,
        output_reserve_ratio=0.0,
        type_cap_ratios={
            "understanding": 0.35,
            "memory": 0.05,
            "history": 0.35,
        },
    ).build(state)

    current = section(context, "understanding:10")
    optional_memory = section(context, "memory:410")
    assert not current.dropped
    assert optional_memory.dropped
    per_type = context.context_manifest["budget"]["per_type"]
    assert per_type["understanding"]["required"] is True
    assert per_type["memory"]["required"] is False
    assert per_type["memory"]["injection_status"] == "OMITTED"


# ===================== 4. 去重：同一 source_id 只注入一次 =====================


def test_same_evidence_source_id_is_injected_once() -> None:
    state = make_state(
        EVIDENCE_QUESTION,
        evidences=[
            evidence(11, "DUP-EVIDENCE original"),
            evidence(11, "DUP-EVIDENCE related", cross=True),
        ],
    )
    state.retrieval_plan = plan_for(EVIDENCE_QUESTION)

    context = ContextBuilder().build(state)
    evidence_sections = [
        item
        for item in context.sections
        if item.name in {"evidence:11", "related_evidence:11"}
    ]
    assert [item.name for item in evidence_sections] == ["evidence:11"]
    entries = context.context_manifest["sources"]
    # 同一 source_id 只允许一个实际注入；另一条必须留下排除原因。
    injected = [
        e
        for e in entries
        if e["source_id"] == "evidence:11"
        and e["injection_status"] in {"SELECTED", "TRUNCATED"}
    ]
    assert len(injected) == 1
    excluded = [
        e
        for e in entries
        if e["type"] == "related_evidence" and e["injection_status"] == "EXCLUDED"
    ]
    assert excluded and excluded[0]["reason"] == "duplicate_source"


# ===================== 5. 语义冗余：Understanding 完整，历史材料降细节 =====================


def test_semantic_redundancy_keeps_both_sides_but_lowers_detail() -> None:
    shared = "TLB-OVERLAP-MARKER " + "用户认为 TLB 缓存页表项" * 30
    state = make_state(
        EVIDENCE_QUESTION,
        understandings=[understanding(summary=shared)],
        memories=[memory(420, shared)],
        evidences=[evidence(12, shared)],
    )
    state.retrieval_plan = plan_for(EVIDENCE_QUESTION)

    context = ContextBuilder().build(state)
    current = section(context, "understanding:10")
    memory_section = section(context, "memory:420")
    evidence_section = section(context, "evidence:12")

    assert current.tier == "full"
    assert "TLB-OVERLAP-MARKER" in current.content
    # 冗余只降低细节，不删除，也不把两边合并。
    assert not memory_section.dropped and not evidence_section.dropped
    assert memory_section.tier == "structured"
    assert evidence_section.tier == "structured"
    assert "记录于 2026-01-01 09:00" in memory_section.content
    assert "2026-01-02 09:00" in evidence_section.content


# ===================== 6. 冲突：两边保留，来源与时间可核对 =====================


def test_conflict_keeps_both_sides_with_source_and_time() -> None:
    state = make_state(
        PLAIN_QUESTION,
        understandings=[
            understanding(
                summary="CURRENT-STATE-MARKER 用户现在认为 TLB 是页表项缓存",
                updated_at=T1,
            )
        ],
        memories=[
            memory(
                430,
                "HISTORICAL-MEMORY-MARKER 用户过去认为 TLB 缓存整个页表",
                created_at=T0,
            )
        ],
    )
    state.retrieval_plan = plan_for(PLAIN_QUESTION)

    context = ContextBuilder().build(state)
    prompt = system(context)
    assert "CURRENT-STATE-MARKER" in prompt
    assert "HISTORICAL-MEMORY-MARKER" in prompt
    assert "当前快照更新于 2026-01-02 09:00" in prompt
    assert "记录于 2026-01-01 09:00" in prompt
    assert "可能与当前认知冲突" in prompt
    assert "以当前快照为准" in prompt
    # 两边各自成条，不能被合并成一条「用户认为…」的事实。
    assert "CURRENT-STATE-MARKER 用户过去认为" not in prompt


# ===================== 7. Manifest：最小字段且不泄正文 =====================


def test_manifest_has_minimal_fields_and_no_body_or_query() -> None:
    state = make_state(
        "PRIVATE-QUESTION-MARKER TLB 是什么？",
        understandings=[
            understanding(summary="PRIVATE-BODY-MARKER 私有正文内容")
        ],
        memories=[memory(440, "PRIVATE-MEMORY-MARKER 私有记忆")],
    )
    state.retrieval_plan = plan_for("PRIVATE-QUESTION-MARKER TLB 是什么？")

    context = ContextBuilder().build(state)
    manifest = context.context_manifest

    assert set(manifest) == {
        "schema_version",
        "policy_version",
        "intent",
        "sources",
        "budget",
    }
    assert manifest["schema_version"] == 1
    assert manifest["policy_version"] == "d4-2-v1"
    assert manifest["intent"] == "LEARNING_EXPLANATION"
    assert manifest["sources"]

    source_keys = {
        "type",
        "source_id",
        "retrieval_status",
        "injection_status",
        "reason",
        "estimated_tokens",
    }
    for entry in manifest["sources"]:
        assert set(entry) == source_keys
        assert isinstance(entry["estimated_tokens"], int)
        assert entry["estimated_tokens"] >= 0

    budget = manifest["budget"]
    assert {"total", "used", "remaining", "per_type"} <= set(budget)
    assert budget["total"] == 3000
    assert 0 <= budget["used"] <= budget["total"]
    assert budget["remaining"] == max(0, budget["total"] - budget["used"])

    raw = json.dumps(manifest, ensure_ascii=False)
    for forbidden in (
        "PRIVATE-QUESTION-MARKER",
        "PRIVATE-BODY-MARKER",
        "PRIVATE-MEMORY-MARKER",
        "preview",
        "system_instruction",
    ):
        assert forbidden not in raw


# ===================== 8. D4-2A 回归：计划存在时也不改坏措辞 =====================


def test_degraded_status_wording_survives_type_budgeting() -> None:
    state = make_state(
        PLAIN_QUESTION,
        understanding_status="DEGRADED",
        understanding_warning="学习认知检索失败：backend 500",
    )
    state.retrieval_plan = plan_for(PLAIN_QUESTION)

    prompt = system(ContextBuilder().build(state))
    assert "学习认知检索：DEGRADED" in prompt
    assert "无法确认用户是否已有认知记录" in prompt
    assert "不要声称用户没学过" in prompt
    assert "没有检索到任何历史认知记录" not in prompt


def test_not_queried_does_not_imply_no_history_with_plan() -> None:
    state = make_state("你好")
    state.retrieval_plan = plan_for("你好")

    prompt = system(ContextBuilder().build(state))
    assert "没有检索到任何历史认知记录" not in prompt
    assert "结果确实为空" not in prompt


# ===================== 9. Revision 只在 LEARNING_REVIEW 注入 =====================


def trajectory() -> LearningTrajectory:
    return LearningTrajectory(
        concept_id=CONCEPT_ID,
        concept_name="TLB",
        current_status=UnderstandingStatus.PARTIAL,
        revisions=[
            UnderstandingRevision(
                id=700,
                concept_id=CONCEPT_ID,
                revision_no=1,
                summary="早期理解",
                misconceptions=[
                    Misconception(
                        text="REVISION-MARKER 早期把 TLB Miss 当成 Page Fault",
                        status=MisconceptionStatus.ACTIVE,
                    )
                ],
                status=UnderstandingStatus.MISCONCEPTION,
                created_at=T0,
            )
        ],
    )


def test_revision_is_not_injected_for_ordinary_explanation() -> None:
    state = make_state(PLAIN_QUESTION, trajectories=[trajectory()])
    state.retrieval_plan = plan_for(PLAIN_QUESTION)

    context = ContextBuilder().build(state)
    assert "REVISION-MARKER" not in system(context)
    excluded = [
        entry
        for entry in context.context_manifest["sources"]
        if entry["type"] == "trajectory"
    ]
    assert excluded
    assert excluded[0]["injection_status"] == "EXCLUDED"
    assert excluded[0]["reason"] == "intent_not_review"


def test_revision_is_injected_for_learning_review() -> None:
    state = make_state(REVIEW_QUESTION, trajectories=[trajectory()])
    state.retrieval_plan = plan_for(REVIEW_QUESTION)

    context = ContextBuilder().build(state)
    assert state.retrieval_plan.intent.value == "LEARNING_REVIEW"
    assert "REVISION-MARKER" in system(context)
    selected = [
        entry
        for entry in context.context_manifest["sources"]
        if entry["type"] == "trajectory"
    ]
    assert selected and selected[0]["injection_status"] in {"SELECTED", "TRUNCATED"}
