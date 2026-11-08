"""D4-2：Retrieval Policy / Planner 的规则、Plan 与最小接入。

这些测试只覆盖三件事：
1. 规则本身（意图、Session 解耦、数据源矩阵）；
2. Plan 真的被 Runtime 执行（查 / 不查的调用计数与状态）；
3. 多概念 Memory 在**现有后端契约**下的适配行为。

不连后端、不调真实模型、不改 ContextBuilder 的排序与预算。
"""

from __future__ import annotations

from collections import Counter

import httpx

from app.context.policy import (
    Intent,
    RetrievalNeed,
    plan_retrieval,
)
from app.llm import build_llm_adapter
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
    MemoryRetrievalResult,
    RelatedConceptResult,
    RelatedConceptStatus,
    RetrievalStatus,
    UnderstandingRetrievalResult,
)
from app.retriever.http_backend import HttpBackendRetriever
from app.runtime import AgentRuntime
from app.schemas import Concept, SessionContext
from tests.helpers import make_request

DERIVATIVE = Concept(id=1, canonical_name="导数")
LIMIT = Concept(id=2, canonical_name="极限")
TLB = Concept(id=10, canonical_name="TLB")
REDIS = Concept(id=20, canonical_name="Redis")
CATALOG = [DERIVATIVE, LIMIT, TLB, REDIS]


def plan(message: str, concept_ids=(), *, session=None, catalog=None):
    return plan_retrieval(
        user_message=message,
        concept_ids=list(concept_ids),
        session=session,
        space_concepts=CATALOG if catalog is None else catalog,
    )


# ===================== 六种意图 =====================


def test_six_intents_are_classified_by_rules() -> None:
    assert plan("导数的定义是什么？", [1]).intent is Intent.LEARNING_EXPLANATION
    assert (
        plan("我之前对 TLB 的理解错在哪里？后来怎么纠正的？", [10]).intent
        is Intent.LEARNING_REVIEW
    )
    assert (
        plan(
            "继续",
            [1],
            session=SessionContext(
                id=7, title="导数与极限", goal="理解导数", current_focus="极限"
            ),
        ).intent
        is Intent.CONTINUE_TASK
    )
    assert plan("这份资料里怎么定义导数？", [1]).intent is Intent.KNOWLEDGE_QUERY
    assert plan("我现在的掌握程度怎么样？", [1]).intent is Intent.META_QUESTION
    assert plan("你好", []).intent is Intent.CASUAL


def test_no_concept_substantive_question_falls_back_to_knowledge_query() -> None:
    """没有概念不等于寒暄：它仍然要查用户上传的文档。"""
    decision = plan("今天天气怎么样？", [])

    assert decision.intent is Intent.KNOWLEDGE_QUERY
    assert decision.knowledge.need is RetrievalNeed.REQUIRED
    assert decision.knowledge.triggered is True


# ===================== 数据源矩阵 =====================


def test_explanation_reads_understanding_memory_and_knowledge_only() -> None:
    decision = plan("导数的定义是什么？", [1])

    assert decision.understanding.need is RetrievalNeed.REQUIRED
    assert decision.memory.need is RetrievalNeed.REQUIRED
    assert decision.knowledge.need is RetrievalNeed.REQUIRED
    assert decision.evidence.need is RetrievalNeed.SKIP
    assert decision.evidence_related.need is RetrievalNeed.SKIP
    assert decision.revision.need is RetrievalNeed.SKIP


def test_explicit_evidence_request_adds_raw_evidence() -> None:
    decision = plan("为什么导数的定义是这样？依据是什么", [1])

    assert decision.intent is Intent.LEARNING_EXPLANATION
    assert decision.evidence.need is RetrievalNeed.REQUIRED
    assert decision.evidence_related.need is RetrievalNeed.OPTIONAL
    assert decision.revision.need is RetrievalNeed.SKIP


def test_meta_question_reads_evidence_without_any_trigger_keyword() -> None:
    """「我掌握得怎么样」没有「为什么 / 依据」，也必须看证据，而不是只信快照。"""
    decision = plan("我现在的掌握程度怎么样？", [10])

    assert decision.intent is Intent.META_QUESTION
    assert decision.evidence.need is RetrievalNeed.REQUIRED
    assert decision.revision.need is RetrievalNeed.SKIP


def test_review_intent_uses_revision_not_recent_evidence_by_default() -> None:
    decision = plan("我之前对 TLB 的理解错在哪里？后来怎么纠正的？", [10])

    assert decision.intent is Intent.LEARNING_REVIEW
    assert decision.revision.need is RetrievalNeed.REQUIRED
    assert decision.evidence.need is RetrievalNeed.SKIP
    assert decision.review_required is True


def test_review_with_explicit_evidence_request_keeps_revision_first() -> None:
    decision = plan("复盘一下我之前错在哪，依据是什么？", [10])

    assert decision.intent is Intent.LEARNING_REVIEW
    assert decision.revision.need is RetrievalNeed.REQUIRED
    assert decision.evidence.need is RetrievalNeed.REQUIRED


def test_casual_message_skips_every_retrieval_source() -> None:
    decision = plan("你好", [])

    assert decision.intent is Intent.CASUAL
    for source in (
        decision.understanding,
        decision.memory,
        decision.evidence,
        decision.revision,
        decision.knowledge,
    ):
        assert source.need is RetrievalNeed.SKIP
        assert source.triggered is False


def test_no_concept_anchor_forces_personal_sources_to_skip() -> None:
    decision = plan("这份资料里怎么定义导数？", [])

    assert decision.intent is Intent.KNOWLEDGE_QUERY
    assert decision.understanding.need is RetrievalNeed.SKIP
    assert decision.memory.need is RetrievalNeed.SKIP
    assert decision.evidence.need is RetrievalNeed.SKIP
    assert decision.revision.need is RetrievalNeed.SKIP
    assert decision.knowledge.need is RetrievalNeed.REQUIRED


# ===================== Session 解耦（D4-2C 缺口） =====================


def test_session_goal_is_injected_when_concepts_match() -> None:
    decision = plan(
        "TLB 是什么？",
        [10],
        session=SessionContext(
            id=1,
            title="TLB 快表",
            goal="理解 TLB",
            current_focus="TLB 与页表的关系",
        ),
    )

    assert decision.session.concept_match is True
    assert decision.session.inject_goal is True
    assert decision.session.inject_current_focus is True
    assert decision.session.reason == "session_concept_match"


def test_session_goal_is_stripped_on_concept_mismatch() -> None:
    """SessionResolver 可能把新话题挂到旧 Session；Policy 不能依赖「已挂靠」。"""
    decision = plan(
        "TLB 是什么？",
        [10],
        session=SessionContext(
            id=2,
            title="Redis 缓存",
            goal="掌握 Redis 淘汰策略",
            current_focus="LRU 与 LFU 的区别",
        ),
    )

    assert decision.session.concept_match is False
    assert decision.session.inject_goal is False
    assert decision.session.inject_current_focus is False
    assert decision.session.inject_recent_turns is True
    assert decision.session.reason == "session_concept_mismatch"


def test_session_without_recognizable_concepts_stays_injected() -> None:
    """无法判断时宁可保留：旧 Session 的 goal 不匹配，也比误删真实目标安全。"""
    decision = plan(
        "TLB 是什么？",
        [10],
        session=SessionContext(id=3, title="活动一", goal="SESSION-GOAL"),
    )

    assert decision.session.concept_match is None
    assert decision.session.inject_goal is True
    assert decision.session.reason == "session_frame_undetermined"


def test_casual_message_does_not_inject_session_frame_even_if_attached() -> None:
    decision = plan(
        "谢谢",
        [],
        session=SessionContext(id=4, title="TLB 快表", goal="理解 TLB"),
    )

    assert decision.intent is Intent.CASUAL
    assert decision.session.inject_goal is False
    assert decision.session.inject_current_focus is False


# ===================== Plan 结构 =====================


def test_plan_is_structured_and_has_a_reason_for_every_decision() -> None:
    decision = plan("为什么导数的定义是这样？依据是什么", [1, 2])
    payload = decision.as_dict()

    assert payload["policyVersion"] == "d4-2-v1"
    assert payload["intent"] == "LEARNING_EXPLANATION"
    assert payload["concepts"] == [1, 2]
    assert payload["memoryConceptIds"] == [1, 2]
    assert payload["memoryScopePriority"] == ["CONCEPT", "SPACE", "GLOBAL"]
    for name in (
        "understanding",
        "memory",
        "evidence",
        "evidenceRelated",
        "revision",
        "knowledge",
    ):
        assert payload["needs"][name]["reason"]
        assert isinstance(payload["needs"][name]["triggered"], bool)
    assert payload["session"]["reason"]


def test_multi_concept_memory_keeps_all_concept_ids() -> None:
    decision = plan("导数和极限有什么关系？", [1, 2])

    assert decision.memory.triggered is True
    assert decision.memory_concept_ids == (1, 2)


# ===================== Runtime 接线 =====================


class PolicyRetriever:
    name = "policy-fake"

    def __init__(
        self,
        concepts: list[Concept] | None = None,
        *,
        understanding_status: RetrievalStatus = RetrievalStatus.EMPTY,
        memory: MemoryRetrievalResult | None = None,
    ) -> None:
        self.concepts = list(concepts or [])
        self.understanding_status = understanding_status
        self.memory = memory or MemoryRetrievalResult(status=RetrievalStatus.EMPTY)
        self.calls: Counter[str] = Counter()
        self.memory_concept_ids: list[list[int]] = []

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return list(self.concepts)

    async def get_understandings(self, user_id, space_id, concept_ids):
        self.calls["understanding"] += 1
        return UnderstandingRetrievalResult(status=self.understanding_status)

    async def get_memories(self, user_id, space_id, concept_ids, query):
        self.calls["memory"] += 1
        self.memory_concept_ids.append(list(concept_ids))
        return self.memory

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        self.calls["knowledge"] += 1
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(self, user_id, space_id, concept_ids, limit):
        self.calls["evidence"] += 1
        return EvidenceRetrievalResult.from_items([])

    async def get_related_concepts(self, *args, **kwargs) -> RelatedConceptResult:
        self.calls["related"] += 1
        return RelatedConceptResult(status=RelatedConceptStatus.EMPTY)

    async def get_revisions(self, user_id, concept_ids):
        self.calls["revision"] += 1
        return {concept_id: [] for concept_id in concept_ids}

    async def get_evidence_details(self, user_id, concept_ids, evidence_ids):
        return []

    async def aclose(self) -> None:
        return None


def _runtime(settings) -> AgentRuntime:
    return AgentRuntime(settings, build_llm_adapter(settings))


def _system(prepared) -> str:
    return prepared.context.messages[0].content


async def test_runtime_executes_policy_and_skips_casual_retrieval(settings) -> None:
    retriever = PolicyRetriever([TLB])
    prepared = await _runtime(settings).prepare(
        make_request("你好"), retriever=retriever
    )

    assert retriever.calls == Counter()
    assert prepared.state.retrieval_plan is not None
    assert prepared.state.retrieval_plan.intent is Intent.CASUAL
    assert prepared.state.knowledge_status == "NOT_RETRIEVED"
    assert prepared.state.understanding_status == "NOT_QUERIED"
    assert prepared.state.memory_status == "NOT_QUERIED"
    assert prepared.state.revision_status == "NOT_QUERIED"
    assert prepared.state.evidence_status == "NOT_RETRIEVED"
    # 「没查」不能渲染成「没有记录」。
    assert "没有检索到任何历史认知记录" not in _system(prepared)


async def test_runtime_strips_stale_session_frame_on_concept_mismatch(settings) -> None:
    retriever = PolicyRetriever([TLB, REDIS])
    prepared = await _runtime(settings).prepare(
        make_request(
            "TLB 是什么？",
            session=SessionContext(
                id=11,
                title="Redis 缓存",
                goal="SESSION-STALE-GOAL",
                current_focus="SESSION-STALE-FOCUS",
            ),
        ),
        retriever=retriever,
    )

    prompt = _system(prepared)
    assert "SESSION-STALE-GOAL" not in prompt
    assert "SESSION-STALE-FOCUS" not in prompt
    assert "本阶段学习目标" not in prompt
    assert prepared.state.session is not None
    assert prepared.state.session.id == 11
    assert prepared.state.retrieval_plan.session.concept_match is False


async def test_runtime_keeps_matching_session_frame(settings) -> None:
    retriever = PolicyRetriever([TLB, REDIS])
    prepared = await _runtime(settings).prepare(
        make_request(
            "TLB 是什么？",
            session=SessionContext(id=12, title="TLB 快表", goal="SESSION-LIVE-GOAL"),
        ),
        retriever=retriever,
    )

    assert "SESSION-LIVE-GOAL" in _system(prepared)
    assert prepared.state.retrieval_plan.session.inject_goal is True


async def test_runtime_meta_question_queries_evidence_without_keyword(settings) -> None:
    retriever = PolicyRetriever([TLB])
    prepared = await _runtime(settings).prepare(
        make_request("我 TLB 的掌握程度怎么样？"), retriever=retriever
    )

    assert prepared.state.retrieval_plan.intent is Intent.META_QUESTION
    assert retriever.calls["understanding"] == 1
    assert retriever.calls["memory"] == 1
    assert retriever.calls["evidence"] == 1
    assert retriever.calls["revision"] == 0
    assert prepared.state.revision_status == "NOT_QUERIED"


async def test_runtime_review_intent_reads_revision_not_recent_evidence(settings) -> None:
    retriever = PolicyRetriever([TLB])
    prepared = await _runtime(settings).prepare(
        make_request("我之前对 TLB 的理解错在哪里？后来怎么纠正的？"),
        retriever=retriever,
    )

    assert prepared.state.retrieval_plan.intent is Intent.LEARNING_REVIEW
    assert retriever.calls["revision"] == 1
    assert retriever.calls["evidence"] == 0
    assert prepared.state.history_review_requested is True


async def test_degraded_status_survives_policy_execution(settings) -> None:
    """D4-2A 回归：Policy 只决定「查不查」，不能把 DEGRADED 压成 EMPTY。"""
    retriever = PolicyRetriever(
        [TLB],
        understanding_status=RetrievalStatus.DEGRADED,
        memory=MemoryRetrievalResult(
            status=RetrievalStatus.DEGRADED,
            detail="UNAVAILABLE",
            warning="记忆检索不可用，无法确认是否有相关记忆。",
        ),
    )
    prepared = await _runtime(settings).prepare(
        make_request("TLB 是什么？"), retriever=retriever
    )

    assert prepared.state.understanding_status == "DEGRADED"
    assert prepared.state.memory_status == "DEGRADED"
    assert prepared.state.memory_detail == "UNAVAILABLE"
    assert prepared.state.evidence_status == "NOT_RETRIEVED"
    system = _system(prepared)
    assert "没有检索到任何历史认知记录" not in system


# ===================== 多概念 Memory 适配 =====================


def _http_retriever(handler) -> HttpBackendRetriever:
    retriever = HttpBackendRetriever(
        "http://backend.test", auth_token="user-jwt", memory_top_k=5
    )
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever


def _memory(memory_id: int, content: str = "记忆") -> dict:
    return {
        "id": memory_id,
        "scopeType": "CONCEPT",
        "memoryType": "LEARNING",
        "content": content,
        "status": "ACTIVE",
        "importance": 0.5,
    }


async def test_multi_concept_memory_queries_each_concept_and_merges() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        concept_id = request.url.params.get("conceptId")
        seen.append(concept_id)
        sets = {
            "1": [11, 12],
            "2": [12, 21],
        }
        items = [{"memory": _memory(memory_id)} for memory_id in sets[concept_id]]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"status": "OK", "items": items, "warnings": []},
            },
        )

    retriever = _http_retriever(handler)
    try:
        result = await retriever.get_memories(7, 9, [1, 2], "导数和极限")
    finally:
        await retriever.aclose()

    # 后端只接受单个 conceptId：每个概念各发一次，而不是丢掉第二个。
    assert sorted(seen) == ["1", "2"]
    # 轮转交错 + 按 memory.id 去重。
    assert [item.id for item in result.items] == [11, 12, 21]
    assert result.status is RetrievalStatus.SUCCESS
    assert result.warning is None


async def test_multi_concept_memory_partial_failure_stays_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("conceptId") == "1":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"status": "OK", "items": [{"memory": _memory(11)}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "status": "UNAVAILABLE",
                    "items": [],
                    "warnings": ["向量检索失败：Agent 不可用"],
                },
            },
        )

    retriever = _http_retriever(handler)
    try:
        result = await retriever.get_memories(7, 9, [1, 2], "导数和极限")
    finally:
        await retriever.aclose()

    assert [item.id for item in result.items] == [11]
    assert result.status is RetrievalStatus.DEGRADED
    assert result.detail == "OK,UNAVAILABLE"
    assert "conceptIds=[1, 2]" in (result.warning or "")
    assert "无法确认" in (result.warning or "")


async def test_single_concept_memory_keeps_one_request_contract() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("conceptId"))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"status": "OK", "items": [{"memory": _memory(11)}]},
            },
        )

    retriever = _http_retriever(handler)
    try:
        result = await retriever.get_memories(7, 9, [3], "递归")
    finally:
        await retriever.aclose()

    assert seen == ["3"]
    assert [item.id for item in result.items] == [11]
    assert result.status is RetrievalStatus.SUCCESS


async def test_empty_concept_memory_still_sends_one_request_without_concept() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("conceptId"))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"status": "OK", "items": []}},
        )

    retriever = _http_retriever(handler)
    try:
        result = await retriever.get_memories(7, 9, [], "递归")
    finally:
        await retriever.aclose()

    assert seen == [None]
    assert result.status is RetrievalStatus.EMPTY
