"""学习轨迹的选取、精确取证与上下文渲染。

覆盖阶段 5.2 的三条硬约束：
1. 复盘问题按 Revision 的 trigger/reversal 关联找回旧证据，而不是取最近 N 条；
2. 历史误区必须标成「已纠正，历史」，不能写成当前 ACTIVE；
3. 普通知识问题不读取、不注入完整历史轨迹。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.context.builder import ContextBuilder
from app.context.trajectory import (
    attach_evidences,
    build_trajectories,
    collect_evidence_ids,
    historical_misconceptions,
)
from app.llm import build_llm_adapter
from app.retriever.base import KnowledgeResult, KnowledgeStatus
from app.runtime import AgentRuntime
from app.schemas import (
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceSourceItem,
    EvidenceType,
    Misconception,
    MisconceptionStatus,
    Understanding,
    UnderstandingRevision,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace, TraceEventType
from tests.helpers import make_request

T0 = datetime(2026, 10, 26, 23, 30)
MISCONCEPTION_TEXT = "TLB 就是页表本身，它缓存的是整个页表"
CORRECT_TEXT = "TLB 缓存的是页表项，不是整个页表"


def understanding() -> Understanding:
    return Understanding(
        id=500,
        concept_id=10,
        concept_name="TLB",
        summary="已理解：TLB 缓存的是页表项。",
        known_parts=[CORRECT_TEXT],
        misconceptions=[],
        status=UnderstandingStatus.DEMONSTRATED,
    )


def revision(
    revision_no: int,
    status: UnderstandingStatus,
    *,
    misconceptions: list[Misconception] | None = None,
    trigger: int | None = None,
) -> UnderstandingRevision:
    return UnderstandingRevision(
        id=1000 + revision_no,
        concept_id=10,
        revision_no=revision_no,
        summary=None,
        known_parts=[],
        misconceptions=misconceptions or [],
        status=status,
        trigger_evidence_id=trigger,
        created_at=T0 + timedelta(minutes=revision_no),
    )


def revisions() -> list[UnderstandingRevision]:
    active = Misconception(
        text=MISCONCEPTION_TEXT,
        status=MisconceptionStatus.ACTIVE,
        reversal_evidence_ids=[],
    )
    pending = Misconception(
        text=MISCONCEPTION_TEXT,
        status=MisconceptionStatus.PENDING,
        reversal_evidence_ids=[103],
    )
    return [
        revision(1, UnderstandingStatus.EXPOSED, trigger=101),
        revision(
            2,
            UnderstandingStatus.MISCONCEPTION,
            misconceptions=[active],
            trigger=102,
        ),
        revision(
            3,
            UnderstandingStatus.MISCONCEPTION,
            misconceptions=[pending],
            trigger=103,
        ),
        revision(4, UnderstandingStatus.DEMONSTRATED, trigger=104),
    ]


def evidences() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id=101,
            concept_id=10,
            evidence_type=EvidenceType.USER_EXPLANATION,
            observation="我理解 TLB 就是页表本身，它缓存的是整个页表",
            correctness=EvidenceCorrectness.PARTIAL,
            created_at=T0,
            sources=[EvidenceSourceItem(source_type="MESSAGE", source_id=165)],
        ),
        EvidenceItem(
            id=102,
            concept_id=10,
            evidence_type=EvidenceType.USER_CORRECTION,
            observation=MISCONCEPTION_TEXT,
            correctness=EvidenceCorrectness.INCORRECT,
            created_at=T0 + timedelta(minutes=1),
            sources=[EvidenceSourceItem(source_type="HIGHLIGHT", source_id=29)],
        ),
        EvidenceItem(
            id=103,
            concept_id=10,
            evidence_type=EvidenceType.USER_CORRECTION,
            observation="TLB 缓存的是页表项",
            correctness=EvidenceCorrectness.CORRECT,
            created_at=T0 + timedelta(minutes=2),
            sources=[EvidenceSourceItem(source_type="HIGHLIGHT", source_id=30)],
        ),
        EvidenceItem(
            id=104,
            concept_id=10,
            evidence_type=EvidenceType.TEST_RESULT,
            observation=CORRECT_TEXT,
            correctness=EvidenceCorrectness.CORRECT,
            created_at=T0 + timedelta(minutes=3),
            sources=[EvidenceSourceItem(source_type="TEST_ANSWER", source_id=4)],
        ),
    ]


def test_revision_links_select_the_original_error_and_corrections():
    trajectory = build_trajectories([understanding()], {10: revisions()})[0]

    assert [item.revision_no for item in trajectory.revisions] == [1, 2, 3, 4]
    assert historical_misconceptions(trajectory) == [MISCONCEPTION_TEXT]
    # trigger 101/102/103/104 + reversal 103，去重后仍是这四条
    assert collect_evidence_ids([trajectory], [understanding()]) == [101, 102, 103, 104]

    attach_evidences([trajectory], evidences())
    assert [item.id for item in trajectory.evidences] == [101, 102, 103, 104]


def test_review_context_marks_history_as_resolved_and_keeps_current_state():
    trajectory = build_trajectories([understanding()], {10: revisions()})[0]
    attach_evidences([trajectory], evidences())
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message="我之前对 TLB 的理解错在哪里？后来怎么纠正的？",
        understandings=[understanding()],
        trajectories=[trajectory],
        evidences=evidences(),
        trace=RunTrace(run_id=1),
    )

    system = ContextBuilder().build(state).messages[0].content

    assert "学习轨迹（历史" in system
    assert "当前状态（来自持久化快照）：已能讲清" in system
    assert "[已纠正，历史]" in system
    assert "当时 ACTIVE" in system
    assert "后续正确证据（2 条）" in system
    assert "[答错/用户纠正批注]" in system
    assert "来源：批注" in system
    assert "来源：测试回答" in system
    # 面向用户的历史来源不泄露数据库证据 / 来源 id
    assert "批注#29" not in system
    assert "测试回答#4" not in system
    assert "#101" not in system
    assert "我理解 TLB 就是页表本身" in system
    assert CORRECT_TEXT in system
    # 记录没有写出的动机 / 外部资料使用情况必须明确标为不可确认
    assert "以上是全部可核对记录" in system
    assert "是否借助外部资料" in system
    # 历史误区不能再用「当前存在」描述
    assert "当前存在]" not in system
    # 轨迹证据已按时间渲染，不再重复成独立 evidence 区块
    system_sections = ContextBuilder().build(state).sections
    assert not any(section.name.startswith("evidence:") for section in system_sections)


def test_plain_question_does_not_render_history_section():
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message="TLB 缓存的是什么？",
        understandings=[understanding()],
        trace=RunTrace(run_id=1),
    )

    context = ContextBuilder().build(state)
    assert context.messages[-1].content == "TLB 缓存的是什么？"
    assert "学习轨迹" not in context.messages[0].content


def _review_retriever(settings):
    class ReviewRetriever:
        name = "review"
        calls = {"revisions": 0, "details": 0, "recent": 0}

        async def list_concepts(self, space_id):
            return [Concept(id=10, canonical_name="TLB", display_name="TLB（快表）")]

        async def get_understandings(self, user_id, space_id, concept_ids):
            return [understanding()]

        async def get_memories(self, *args):
            return []

        async def get_knowledge(self, query):
            return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

        async def get_evidences(self, *args):
            self.calls["recent"] += 1
            raise AssertionError("复盘不应再按时间取最近证据")

        async def get_revisions(self, user_id, concept_ids):
            self.calls["revisions"] += 1
            return {10: revisions()}

        async def get_evidence_details(self, user_id, concept_ids, evidence_ids):
            self.calls["details"] += 1
            return [item for item in evidences() if item.id in evidence_ids]

        async def aclose(self):
            return None

    return ReviewRetriever()


async def test_runtime_uses_revision_links_only_for_review_questions(settings, monkeypatch):
    retriever = _review_retriever(settings)
    monkeypatch.setattr("app.runtime.build_retriever", lambda *args, **kwargs: retriever)
    runtime = AgentRuntime(settings, build_llm_adapter(settings))

    prepared = await runtime.prepare(
        make_request("我之前对 TLB 的理解错在哪里？后来怎么纠正的？")
    )
    detail = next(
        event.detail
        for event in prepared.trace.events
        if event.type is TraceEventType.RETRIEVE
    )

    assert detail["trajectoryRevisions"] == 4
    assert detail["trajectoryEvidenceIds"] == [101, 102, 103, 104]
    assert retriever.calls == {"revisions": 1, "details": 1, "recent": 0}
    system = prepared.context.messages[0].content
    assert "[已纠正，历史]" in system
    assert "我理解 TLB 就是页表本身" in system


async def test_runtime_does_not_read_revisions_for_plain_question(settings, monkeypatch):
    class PlainRetriever:
        name = "plain"
        revisions_called = False

        async def list_concepts(self, space_id):
            return [Concept(id=10, canonical_name="TLB", display_name="TLB（快表）")]

        async def get_understandings(self, user_id, space_id, concept_ids):
            return [understanding()]

        async def get_memories(self, *args):
            return []

        async def get_knowledge(self, query):
            return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

        async def get_evidences(self, *args):
            return []

        async def get_revisions(self, user_id, concept_ids):
            self.revisions_called = True
            return {10: revisions()}

        async def get_evidence_details(self, *args):
            raise AssertionError("普通问题不应精确回查历史证据")

        async def aclose(self):
            return None

    retriever = PlainRetriever()
    monkeypatch.setattr("app.runtime.build_retriever", lambda *args, **kwargs: retriever)
    runtime = AgentRuntime(settings, build_llm_adapter(settings))

    prepared = await runtime.prepare(make_request("TLB 缓存的是什么？"))

    assert retriever.revisions_called is False
    assert prepared.state.trajectories == []
    assert "学习轨迹" not in prepared.context.messages[0].content
