"""E2：结构化 Concept Resolver 的服务端校验、按需调用与 E1 链接边界。

这些测试不连真实 LLM / MySQL / Qdrant：
- Resolver 的输出由 ScriptedLLM 精确控制；
- Runtime 路径用 FakeRetriever 控制概念、关系与 Evidence；
- 最后一个用例用真实 MockLLMAdapter 跑通降级链路（Mock 返回的不是 JSON）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import pytest

from app.config import Settings
from app.llm.base import ChatMessage, LLMError
from app.llm.mock import MockLLMAdapter
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeResult,
    KnowledgeStatus,
    RelatedConceptResult,
    RelatedConceptStatus,
)
from app.retriever.concept_resolver import (
    ConceptResolver,
    ResolverStatus,
    classify_intent,
    decide_resolver_call,
)
from app.runtime import AgentRuntime, _select_expansion_concepts
from app.schemas import (
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    RelatedConcept,
)
from app.trace import TraceEventType
from tests.helpers import make_request


def concept(concept_id: int, name: str) -> Concept:
    return Concept(id=concept_id, canonical_name=name)


def evidence(evidence_id: int, concept_id: int, text: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        concept_id=concept_id,
        observation=text,
        correctness=EvidenceCorrectness.CORRECT,
    )


def resolver_json(
    *,
    current: list[tuple[int, float]] | None = None,
    related: list[tuple[int, float]] | None = None,
    needs_history: bool = False,
    intent: str = "knowledge_question",
) -> str:
    def rows(items: list[tuple[int, float]]) -> list[dict[str, object]]:
        return [
            {"conceptId": concept_id, "confidence": confidence, "reason": "测试"}
            for concept_id, confidence in items
        ]

    return json.dumps(
        {
            "current_concepts": rows(current or []),
            "related_concepts": rows(related or []),
            "needs_history_reference": needs_history,
            "intent": intent,
        },
        ensure_ascii=False,
    )


class ScriptedLLM:
    """确定性 LLM：complete 返回预先写好的 JSON，stream 返回固定回答。"""

    name = "scripted"

    def __init__(
        self,
        completion: str | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.completion = completion or resolver_json()
        self.error = error
        self.delay = delay
        self.complete_calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "回答"

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        self.complete_calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.completion

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    name = "fake"

    def __init__(
        self,
        concepts: list[Concept],
        *,
        related: RelatedConceptResult | None = None,
        evidences: dict[int, list[EvidenceItem]] | None = None,
    ) -> None:
        self.concepts = concepts
        self.related = related or RelatedConceptResult(status=RelatedConceptStatus.EMPTY)
        self.evidences = evidences or {}
        self.related_calls: list[dict[str, object]] = []
        self.evidence_calls: list[tuple[list[int], int]] = []

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return list(self.concepts)

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_knowledge(self, query) -> KnowledgeResult:
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        self.evidence_calls.append((list(concept_ids), limit))
        found: list[EvidenceItem] = []
        for concept_id in concept_ids:
            found.extend(self.evidences.get(concept_id, []))
        return EvidenceRetrievalResult.from_items(found[:limit])

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        self.related_calls.append(
            {
                "concept_ids": list(concept_ids),
                "relation_types": relation_types,
                "limit": limit,
            }
        )
        return self.related

    async def get_revisions(self, user_id: int, concept_ids: list[int]):
        return {}

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ):
        return []

    async def aclose(self) -> None:
        return None


def build_runtime(
    settings: Settings,
    llm,
    *,
    timeout_seconds: float = 1.0,
    max_prompt_tokens: int = 4000,
) -> AgentRuntime:
    resolver = ConceptResolver(
        llm,
        confidence_threshold=settings.concept_resolver_confidence_threshold,
        max_per_field=settings.concept_resolver_max_concepts_per_field,
        timeout_seconds=timeout_seconds,
        max_tokens=settings.concept_resolver_max_tokens,
        max_prompt_tokens=max_prompt_tokens,
        max_candidates=settings.concept_resolver_max_candidate_concepts,
        history_turns=settings.concept_resolver_history_turns,
        history_chars_per_message=settings.concept_resolver_history_chars_per_message,
    )
    return AgentRuntime(settings, llm, concept_resolver=resolver)


async def prepare(runtime: AgentRuntime, retriever: FakeRetriever, question: str, **kwargs):
    request = make_request(question, **kwargs)
    return await runtime.prepare(request, retriever=retriever)


def trace_detail(prepared, event_type: TraceEventType) -> dict | None:
    for event in prepared.trace.events:
        if event.type is event_type:
            return event.detail
    return None


# ---------- T5.1 / T5.2：编造与跨 Space 的 conceptId 一律丢弃 ----------


async def test_fabricated_concept_id_is_dropped(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(current=[(999, 0.99)], related=[(1000, 0.9)], intent="prerequisite_question")
    )
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="求导前面的基础是什么？",
        history=[],
        candidates=[concept(101, "极限"), concept(102, "导数")],
        allow_history_reference=True,
    )

    assert decision.status is ResolverStatus.SUCCESS
    assert decision.current_concepts == []
    assert decision.related_concepts == []
    assert any("999" in reason for reason in decision.dropped)
    assert any("1000" in reason for reason in decision.dropped)


async def test_cross_space_concept_id_is_dropped(settings) -> None:
    """Space 1 的候选里没有 901；即使 Resolver 返回它，也不能进入检索。"""
    llm = ScriptedLLM(
        resolver_json(current=[(901, 0.95)], intent="prerequisite_question")
    )
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="趋近的基础是什么？",
        history=[],
        candidates=[concept(101, "极限"), concept(102, "导数")],
        allow_history_reference=True,
    )

    assert decision.current_concepts == []
    assert any("901" in reason and "当前 Space" in reason for reason in decision.dropped)


# ---------- T5.3：置信度阈值 ----------


async def test_confidence_below_threshold_is_filtered(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(current=[(101, 0.59), (102, 0.6)], intent="prerequisite_question")
    )
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="求导前面的基础是什么？",
        history=[],
        candidates=[concept(101, "极限"), concept(102, "导数")],
        allow_history_reference=True,
    )

    assert [item.concept_id for item in decision.current_concepts] == [102]
    assert any("低于阈值" in reason for reason in decision.dropped)


# ---------- T5.4：current / related 字段分离 ----------


async def test_current_and_related_are_separate_fields(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(
            current=[(102, 0.9)],
            related=[(101, 0.8), (102, 0.95)],
            intent="prerequisite_question",
        )
    )
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="求导前面的基础是什么？",
        history=[],
        candidates=[concept(101, "极限"), concept(102, "导数")],
        allow_history_reference=True,
    )

    assert [item.concept_id for item in decision.current_concepts] == [102]
    # 与 current 重复的 102 从 related 移除，但 101 仍留在 related。
    assert [item.concept_id for item in decision.related_concepts] == [101]
    assert any("与 current 重复" in reason for reason in decision.dropped)


# ---------- T5.5：规则命中 + 普通知识问题 → 不调用 ----------


async def test_rule_hit_normal_question_does_not_call_resolver(settings) -> None:
    llm = ScriptedLLM()
    retriever = FakeRetriever([concept(102, "导数")])
    prepared = await prepare(build_runtime(settings, llm), retriever, "导数的定义是什么")

    assert prepared.state.resolver_status == "NOT_CALLED"
    assert prepared.state.resolver_call_reason == "rule_hit_normal_intent"
    assert llm.complete_calls == []
    assert TraceEventType.CONCEPT_RESOLVE.value not in prepared.trace.event_types()
    # 未调用路径不能被记成降级。
    assert prepared.context.resolver_status == "NOT_CALLED"


# ---------- T5.6：规则未命中 + 前置问题 → 调用，current 进入 concept_ids ----------


async def test_rule_miss_prerequisite_calls_resolver_and_merges_current(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(current=[(102, 0.92)], related=[(101, 0.8)], intent="prerequisite_question")
    )
    retriever = FakeRetriever([concept(101, "极限"), concept(102, "导数")])
    prepared = await prepare(
        build_runtime(settings, llm),
        retriever,
        "求导前面的基础是什么？为什么",
    )
    state = prepared.state

    assert state.resolver_status == "SUCCESS"
    assert state.concept_ids == [102]
    assert state.resolver_current_concept_ids == [102]
    assert state.resolver_related_concept_ids == [101]
    assert state.detected_concepts[0].matched_on.startswith("resolver:")
    assert len(llm.complete_calls) == 1
    detail = trace_detail(prepared, TraceEventType.CONCEPT_RESOLVE)
    assert detail is not None
    assert detail["status"] == "SUCCESS"
    assert detail["currentConceptIds"] == [102]


# ---------- T5.7：超时 / 5xx / 非法 JSON → DEGRADED，主链路继续 ----------


async def test_resolver_timeout_degrades_without_blocking(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(current=[(102, 0.9)], intent="prerequisite_question"),
        delay=0.5,
    )
    retriever = FakeRetriever([concept(101, "极限"), concept(102, "导数")])
    prepared = await prepare(
        build_runtime(settings, llm, timeout_seconds=0.05),
        retriever,
        "求导前面的基础是什么？",
    )
    state = prepared.state

    assert state.resolver_status == "DEGRADED"
    assert "超时" in (state.resolver_warning or "")
    assert state.concept_ids == []  # 规则未命中，Resolver 降级后不补概念
    assert state.evidence_status == "NOT_RETRIEVED"
    assert any("概念语义解析未完成" in warning for warning in prepared.context.warnings)


@pytest.mark.parametrize(
    "llm",
    [
        ScriptedLLM(error=LLMError("upstream 503")),
        ScriptedLLM(completion="抱歉，我只能用自然语言回答。"),
    ],
    ids=["llm_error", "invalid_json"],
)
async def test_resolver_failure_degrades_without_blocking(settings, llm) -> None:
    retriever = FakeRetriever([concept(101, "极限"), concept(102, "导数")])
    prepared = await prepare(
        build_runtime(settings, llm),
        retriever,
        "求导前面的基础是什么？为什么",
    )
    state = prepared.state

    assert state.resolver_status == "DEGRADED"
    assert state.resolver_warning
    assert state.concept_ids == []
    # 主链路仍然完成了知识检索与上下文组装。
    assert state.knowledge_status == "NO_MATCH"
    assert prepared.context.system_instruction


# ---------- T5.8：related 不能绕过 concept_relation ----------


async def test_related_hint_cannot_bypass_relation_table(settings) -> None:
    """Resolver 返回的 999 在候选集里、但关系表没有它 → 扩展永远选不到它。"""
    llm = ScriptedLLM(
        resolver_json(
            current=[(102, 0.9)],
            related=[(999, 0.99), (101, 0.7)],
            intent="prerequisite_question",
        )
    )
    retriever = FakeRetriever(
        [concept(101, "极限"), concept(102, "导数"), concept(999, "幽灵概念")],
        related=RelatedConceptResult(
            items=[
                RelatedConcept(
                    concept_id=101,
                    concept_name="极限",
                    relation_type="PREREQUISITE",
                    direction="INCOMING",
                    source_concept_id=102,
                )
            ],
            status=RelatedConceptStatus.OK,
        ),
        evidences={
            102: [evidence(1021, 102, "导数证据")],
            101: [evidence(1011, 101, "极限证据")],
            999: [evidence(9991, 999, "幽灵证据")],
        },
    )
    prepared = await prepare(
        build_runtime(settings, llm),
        retriever,
        "求导前面的基础是什么？为什么",
    )
    state = prepared.state

    assert state.resolver_related_concept_ids == [999, 101]
    assert [item.concept_id for item in state.expanded_concepts] == [101]
    # 证据查询只发生两次：原始概念 102 + 关系表确认的 101；从未查 999。
    assert retriever.evidence_calls == [([102], 3), ([101], 2)]
    assert 999 not in {concept_id for ids, _ in retriever.evidence_calls for concept_id in ids}
    detail = trace_detail(prepared, TraceEventType.RETRIEVE)
    assert detail is not None
    assert detail["resolverHintsNotInRelation"] == [999]


def test_hint_only_reorders_real_relation_candidates() -> None:
    """纯函数边界：hint 只能重排真实候选，不能新增、不能改变方向过滤。"""
    incoming = RelatedConcept(
        concept_id=2, relation_type="PREREQUISITE", direction="INCOMING"
    )
    related = RelatedConcept(concept_id=3, relation_type="RELATED", direction="OUTGOING")
    outgoing_prerequisite = RelatedConcept(
        concept_id=4, relation_type="PREREQUISITE", direction="OUTGOING"
    )

    without_hint = _select_expansion_concepts([1], [incoming, related], limit=1)
    with_hint = _select_expansion_concepts(
        [1], [incoming, related, outgoing_prerequisite], limit=1, hint_ids={3}
    )
    ghost_hint = _select_expansion_concepts([1], [incoming, related], limit=1, hint_ids={999})

    assert [item.concept_id for item in without_hint] == [2]
    assert [item.concept_id for item in with_hint] == [3]
    # 拦截方向不合法的候选；不存在的 hint 被忽略。
    assert all(item.concept_id != 4 for item in _select_expansion_concepts(
        [1], [incoming, related, outgoing_prerequisite], limit=3, hint_ids={4}
    ))
    assert [item.concept_id for item in ghost_hint] == [2]


# ---------- 其他确定性校验 ----------


async def test_max_three_concepts_per_field(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(
            current=[(101, 0.9), (102, 0.8), (103, 0.7), (104, 0.6)],
            intent="prerequisite_question",
        )
    )
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="这些概念的关系？",
        history=[],
        candidates=[concept(101, "A"), concept(102, "B"), concept(103, "C"), concept(104, "D")],
        allow_history_reference=True,
    )

    assert len(decision.current_concepts) == 3
    assert any("超过每字段上限" in reason for reason in decision.dropped)


async def test_needs_history_reference_cannot_be_forced(settings) -> None:
    llm = ScriptedLLM(
        resolver_json(current=[(101, 0.9)], needs_history=True, intent="knowledge_question")
    )
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="导数的定义是什么",
        history=[],
        candidates=[concept(101, "A")],
        allow_history_reference=False,
    )

    assert decision.needs_history_reference is False
    assert "不允许依赖历史" in (decision.warning or "")


async def test_unknown_intent_falls_back_to_other(settings) -> None:
    llm = ScriptedLLM(resolver_json(intent="made_up_intent"))
    resolver = build_runtime(settings, llm)._resolver
    decision = await resolver.resolve(
        run_id=1,
        user_id=1,
        space_id=1,
        user_message="导数的定义是什么",
        history=[],
        candidates=[concept(101, "A")],
        allow_history_reference=False,
    )

    assert decision.intent == "other"
    assert "intent" in (decision.warning or "")


def test_call_policy_decision_table() -> None:
    normal = decide_resolver_call(
        "导数的定义是什么", rule_hit_count=1, candidate_count=5
    )
    assert (normal.call, normal.intent, normal.reason) == (
        False,
        "knowledge_question",
        "rule_hit_normal_intent",
    )
    prerequisite = decide_resolver_call(
        "求导前面的基础是什么", rule_hit_count=0, candidate_count=5
    )
    assert (prerequisite.call, prerequisite.intent) == (True, "prerequisite_question")
    assert prerequisite.allow_history_reference is True

    assert decide_resolver_call(
        "上次那个误区是什么", rule_hit_count=0, candidate_count=5
    ).call is True
    assert decide_resolver_call("你好", rule_hit_count=0, candidate_count=5).call is False
    assert decide_resolver_call("继续", rule_hit_count=0, candidate_count=5).call is False
    assert decide_resolver_call("随便聊聊", rule_hit_count=0, candidate_count=5).call is False
    assert decide_resolver_call(
        "导数的定义是什么", rule_hit_count=0, candidate_count=5, enabled=False
    ).call is False


def test_intent_classification_examples() -> None:
    assert classify_intent("导数的前置是什么？") == "prerequisite_question"
    assert classify_intent("我之前对 TLB 的理解错在哪里？后来怎么纠正的？") == "review_request"
    assert classify_intent("你好") == "chitchat"
    assert classify_intent("今天天气怎么样？") == "chitchat"
    assert classify_intent("导数的定义是什么") == "knowledge_question"
    assert classify_intent("那个东西到底是什么？") == "knowledge_question"
    assert classify_intent("嗯") == "chitchat"


# ---------- T5.9：真实降级复现（Mock LLM 走真实 Agent 链路）----------


async def test_real_degradation_keeps_evidence_and_context_semantics(
    settings, caplog
) -> None:
    """MockLLMAdapter.complete 返回散文，经真实 Resolver → DEGRADED。

    要证明三件事：
    1. 原始 Evidence 检索与三态语义不受影响；
    2. 上下文明确提示「概念解析未完成」，不能改写成「用户没有历史」；
    3. 主链路照常结束（done），不是 error。
    """
    caplog.set_level(logging.WARNING, logger="app.retriever.concept_resolver")
    retriever = FakeRetriever(
        [concept(103, "连续性"), concept(101, "极限")],
        evidences={103: [evidence(1031, 103, "你之前解释过连续性。")]},
    )
    runtime = build_runtime(settings, MockLLMAdapter())
    request = make_request("连续性的基础是什么？凭什么")

    prepared = await runtime.prepare(request, retriever=retriever)
    state = prepared.state

    assert state.resolver_status == "DEGRADED"
    assert state.resolver_warning
    assert state.evidence_status == "SUCCESS"
    assert [item.id for item in state.evidences] == [1031]
    assert prepared.context.resolver_status == "DEGRADED"
    instruction = prepared.context.messages[0].content
    assert "本轮概念解析状态" in instruction
    assert "概念语义解析未完成" in instruction
    # 不能把解析降级说成「什么都没有」。
    assert "本次没有检索到任何历史认知记录" not in instruction
    assert "不能据此判断学习者没有相关历史" in instruction
    assert any("概念语义解析未完成" in warning for warning in prepared.context.warnings)
    assert any("概念解析返回非法 JSON" in record.message for record in caplog.records)

    events = [event async for event in runtime.stream(request)]
    assert events[-1].event == "done"
    assert events[-1].data["runId"] == request.run_id
