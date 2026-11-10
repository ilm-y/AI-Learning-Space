"""Runtime 对显式复盘回答的校验、一次重生成与确定性回退。

不联网、不调用真实模型：这里钉死的是生产链路的行为 ——
复盘回答必须完整生成并检查后才推送；普通问题仍然即时流式；
无依据断言不会被原样返回；重建调用最多一次；SSE 事件类型不变。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import AsyncIterator

from app.llm.base import ChatMessage
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
)
from app.runtime import AgentRuntime, _is_history_review
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
from app.trace import TraceEventType
from tests.helpers import make_request

REVIEW_QUESTION = "我之前对 TLB 的理解错在哪里？后来是怎么纠正的？"
PLAIN_QUESTION = "TLB 这块再讲讲。"

BAD_ANSWER = (
    "你很可能通过重读定义 + 对比缓存通用含义迁移了过来，"
    "而且没有外部提示或资料介入。后来你完全答对了。"
)
GOOD_ANSWER = (
    "记录里有你当时的原始解释和两条批注：一开始是「TLB 就是页表本身，"
    "它缓存的是整个页表」，后来批注为「TLB 缓存的是页表项」。"
    "后续还有一次测试回答判为答对。当前状态是已能讲清。"
    "记录里看不出你改变看法的原因，也没有记录是否借助外部资料。"
)


def _understanding() -> Understanding:
    return Understanding(
        id=500,
        concept_id=10,
        concept_name="TLB",
        summary="已理解：TLB 缓存的是页表项。",
        known_parts=["TLB 缓存的是页表项，不是整个页表"],
        status=UnderstandingStatus.DEMONSTRATED,
    )


def _revisions() -> list[UnderstandingRevision]:
    now = datetime(2026, 10, 26, 23, 30)
    return [
        UnderstandingRevision(
            id=1002,
            concept_id=10,
            revision_no=2,
            misconceptions=[
                Misconception(text="TLB 就是页表本身，它缓存的是整个页表", status=MisconceptionStatus.ACTIVE)
            ],
            status=UnderstandingStatus.MISCONCEPTION,
            trigger_evidence_id=102,
            created_at=now + timedelta(minutes=1),
        ),
        UnderstandingRevision(
            id=1004,
            concept_id=10,
            revision_no=4,
            status=UnderstandingStatus.DEMONSTRATED,
            trigger_evidence_id=104,
            created_at=now + timedelta(minutes=3),
        ),
    ]


def _evidences() -> list[EvidenceItem]:
    now = datetime(2026, 10, 26, 23, 30)
    return [
        EvidenceItem(
            id=101,
            concept_id=10,
            evidence_type=EvidenceType.USER_EXPLANATION,
            observation="我理解 TLB 就是页表本身，它缓存的是整个页表",
            correctness=EvidenceCorrectness.PARTIAL,
            created_at=now,
            sources=[EvidenceSourceItem(source_type="MESSAGE", source_id=165)],
        ),
        EvidenceItem(
            id=102,
            concept_id=10,
            evidence_type=EvidenceType.USER_CORRECTION,
            observation="TLB 就是页表本身，它缓存的是整个页表",
            correctness=EvidenceCorrectness.INCORRECT,
            created_at=now + timedelta(minutes=1),
            sources=[EvidenceSourceItem(source_type="HIGHLIGHT", source_id=29)],
        ),
        EvidenceItem(
            id=103,
            concept_id=10,
            evidence_type=EvidenceType.USER_CORRECTION,
            observation="TLB 缓存的是页表项",
            correctness=EvidenceCorrectness.CORRECT,
            created_at=now + timedelta(minutes=2),
            sources=[EvidenceSourceItem(source_type="HIGHLIGHT", source_id=30)],
        ),
        EvidenceItem(
            id=104,
            concept_id=10,
            evidence_type=EvidenceType.TEST_RESULT,
            observation="TLB 缓存的是页表项，不是整个页表",
            correctness=EvidenceCorrectness.CORRECT,
            created_at=now + timedelta(minutes=3),
            sources=[EvidenceSourceItem(source_type="TEST_ANSWER", source_id=4)],
        ),
    ]


class ScriptedLLM:
    name = "scripted"

    def __init__(self, answers: list[list[str]]) -> None:
        self._answers = answers
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self._answers) - 1)
        for chunk in self._answers[index]:
            yield chunk

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return ""

    async def aclose(self) -> None:
        return None


class ReviewRetriever:
    name = "review-retriever"

    def __init__(self, revisions: dict[int, list[UnderstandingRevision]] | None = None) -> None:
        self._revisions = revisions if revisions is not None else {10: _revisions()}

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return [Concept(id=10, canonical_name="TLB", display_name="TLB（快表）")]

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return [_understanding()]

    async def get_memories(self, *args):
        return []

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(self, *args):
        return EvidenceRetrievalResult.from_items(_evidences())

    async def get_revisions(
        self, user_id: int, concept_ids: list[int], *, space_id: int | None = None
    ):
        return self._revisions

    async def get_evidence_details(self, user_id: int, concept_ids: list[int], evidence_ids: list[int]):
        return [item for item in _evidences() if item.id in evidence_ids]

    async def aclose(self) -> None:
        return None


async def _prepared(settings, llm: ScriptedLLM, *, question=REVIEW_QUESTION, revisions=None):
    runtime = AgentRuntime(settings, llm)
    prepared = await runtime.prepare(
        make_request(question), retriever=ReviewRetriever(revisions)
    )
    return runtime, prepared


def _history_guard(prepared) -> dict:
    return next(
        event.detail
        for event in prepared.trace.events
        if event.type is TraceEventType.HISTORY_GUARD
    )


async def test_review_answer_is_validated_and_regenerated_once(settings) -> None:
    llm = ScriptedLLM([[BAD_ANSWER], [GOOD_ANSWER]])
    runtime, prepared = await _prepared(settings, llm)

    try:
        output = "".join([delta async for delta in runtime.generate(prepared)])
    finally:
        await runtime.aclose()

    assert len(llm.calls) == 2
    # 重生成指令追加在 system message 尾部，仍保持 system → user 结构。
    retry_system = llm.calls[1][0].content
    assert "上一次回答包含当前记录无法支撑的断言" in retry_system
    assert llm.calls[1][-1].role == "user"

    assert output == GOOD_ANSWER
    assert output == prepared.state.response
    assert "很可能通过重读定义" not in output

    guard = _history_guard(prepared)
    assert guard["regenerated"] is True
    assert guard["fallback"] is False
    assert guard["remainingIssues"] == []
    assert {issue["kind"] for issue in guard["issues"]} >= {
        "internal_process",
        "external_absence",
    }


async def test_review_falls_back_to_records_when_retry_is_still_unsupported(settings) -> None:
    llm = ScriptedLLM([[BAD_ANSWER], [BAD_ANSWER]])
    runtime, prepared = await _prepared(settings, llm)

    try:
        output = "".join([delta async for delta in runtime.generate(prepared)])
    finally:
        await runtime.aclose()

    assert len(llm.calls) == 2  # 最多一次重生成，不无限重试
    assert "记录中的错误理解" in output
    assert "TLB 就是页表本身，它缓存的是整个页表" in output
    assert "很可能通过重读定义" not in output
    assert "#101" not in output

    guard = _history_guard(prepared)
    assert guard["fallback"] is True
    assert guard["regenerated"] is True
    assert guard["remainingIssues"]


QUOTE_REVIEW_QUESTION = REVIEW_QUESTION + "请引用我当时的原话。"
QUOTE_BAD_ANSWER = (
    "你当时的原话是「TLB 就是页表本身，它缓存的是整个页表」，"
    "而且你完全靠自己理解了过来。"
)


async def test_fallback_keeps_the_quote_the_user_asked_for(settings) -> None:
    """KI-2 回退保护：首答里的可核对原句不能被确定性回退覆盖。"""
    llm = ScriptedLLM([[QUOTE_BAD_ANSWER], [QUOTE_BAD_ANSWER]])
    runtime, prepared = await _prepared(
        settings, llm, question=QUOTE_REVIEW_QUESTION
    )

    try:
        output = "".join([delta async for delta in runtime.generate(prepared)])
    finally:
        await runtime.aclose()

    assert len(llm.calls) == 2
    assert "TLB 就是页表本身，它缓存的是整个页表" in output
    assert "「TLB 就是页表本身，它缓存的是整个页表」" in output
    # 同句中的无依据断言不随原句一起回退。
    assert "完全靠自己" not in output

    guard = _history_guard(prepared)
    assert guard["fallback"] is True
    assert guard["regenerated"] is True


S5B_QUESTION = "回顾一下，我之前对 TCP 慢启动的理解错在哪？请引用我当时的原话。"
S5B_ORIGINAL = "我原来以为慢启动慢是因为它每次只把窗口加一"
# Batch 6 S5-B-1 首答的离线复现：假设列表里的「想通」曾被字面命中。
S5B_FIRST_ANSWER = (
    f"你当时的原话是「{S5B_ORIGINAL}」。"
    "记录里的批注显示，后来改成了「慢启动从 1 开始指数增长」。"
    "至于你为什么会改变看法，比如是自己想通的、看了资料，还是别人指出的，"
    "记录里没有写，我不做推测。"
)


async def test_s5b_hypothesis_list_does_not_trigger_history_guard(settings) -> None:
    """KI-2：S5-B-1 首答的假设列表不再触发 internal_process 重生成。

    现有测试基座是 ScriptedLLM 离线夹具；真实链路需要隔离 MySQL / Qdrant
    与模型密钥，本批未运行，因此这里用同一段首答文本重复 3 次验证守卫行为。
    """
    for attempt in range(3):
        llm = ScriptedLLM([[S5B_FIRST_ANSWER]])
        runtime, prepared = await _prepared(
            settings, llm, question=S5B_QUESTION
        )
        try:
            output = "".join([delta async for delta in runtime.generate(prepared)])
        finally:
            await runtime.aclose()

        guard = _history_guard(prepared)
        assert guard["issues"] == [], (attempt, guard["issues"])
        assert guard["regenerated"] is False, attempt
        assert guard["fallback"] is False, attempt
        assert len(llm.calls) == 1, attempt
        assert S5B_ORIGINAL in output, attempt


async def test_review_without_trajectory_never_denies_the_past(settings) -> None:
    llm = ScriptedLLM(
        [
            ["你以前没有这个错误，应该是通过重读定义理解的。"],
            ["你以前没有这个错误，应该是通过重读定义理解的。"],
        ]
    )
    runtime, prepared = await _prepared(settings, llm, revisions={})

    try:
        output = "".join([delta async for delta in runtime.generate(prepared)])
    finally:
        await runtime.aclose()

    assert prepared.state.trajectories == []
    assert len(llm.calls) == 2
    assert "你以前没有这个错误" not in output
    assert "无法确认" in output
    assert "不能断言" in output

    guard = _history_guard(prepared)
    assert guard["hasTrajectory"] is False
    assert guard["fallback"] is True


async def test_plain_question_still_streams_without_buffering(settings) -> None:
    llm = ScriptedLLM([["第一段。", "第二段。"]])
    runtime, prepared = await _prepared(
        settings, llm, question=PLAIN_QUESTION
    )

    try:
        deltas = [delta async for delta in runtime.generate(prepared)]
    finally:
        await runtime.aclose()

    assert prepared.state.history_review_requested is False
    assert deltas == ["第一段。", "第二段。"]
    assert not any(
        event.type is TraceEventType.HISTORY_GUARD for event in prepared.trace.events
    )
    assert len(llm.calls) == 1


def test_history_review_guard_is_narrower_than_trajectory_retrieval() -> None:
    # 个人历史问题进入缓冲校验。
    assert _is_history_review(REVIEW_QUESTION) is True
    assert _is_history_review("之前错在哪？") is True
    assert _is_history_review("之前哪里理解错了？") is True
    # 只是时间词的知识问题 / 学习安排不能触发复盘缓冲。
    assert _is_history_review("过去十年 CPU 架构有什么变化？") is False
    assert _is_history_review("以前 CPU 架构有哪些常见误解？") is False
    assert _is_history_review("之前学过链表，现在学树。") is False
    assert _is_history_review(PLAIN_QUESTION) is False


async def test_review_question_sets_the_flag(settings) -> None:
    llm = ScriptedLLM([["回答"]])
    runtime, prepared = await _prepared(settings, llm)

    try:
        assert prepared.state.history_review_requested is True
        detail = next(
            event.detail
            for event in prepared.trace.events
            if event.type is TraceEventType.RETRIEVE
        )
        assert detail["historyReviewRequested"] is True
    finally:
        await runtime.aclose()


async def test_review_sse_contract_is_unchanged(settings, monkeypatch) -> None:
    llm = ScriptedLLM([[GOOD_ANSWER]])
    runtime = AgentRuntime(settings, llm)
    monkeypatch.setattr(
        "app.runtime.build_retriever", lambda *args, **kwargs: ReviewRetriever()
    )
    try:
        events = [
            event async for event in runtime.stream(make_request(REVIEW_QUESTION))
        ]
    finally:
        await runtime.aclose()

    assert events[0].event == "meta"
    assert events[-1].event == "done"
    assert all(event.event == "delta" for event in events[1:-1])
    joined = "".join(
        str(event.data["delta"]) for event in events if event.event == "delta"
    )
    assert joined == GOOD_ANSWER
