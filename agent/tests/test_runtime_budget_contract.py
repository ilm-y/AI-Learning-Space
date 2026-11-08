"""Runtime 在预算裁剪下的状态与引用契约。

已有覆盖（本文件不重复）：
- ``test_context_builder.py``：状态区块文案、计数、优先级与「丢弃标签不出现」；
- ``test_citations.py``：``provided_labels_from_context`` 对 dropped section
  的过滤，以及 dropped 标签的非法判定；
- ``test_evidence_guard.py``：预算状态不被 Guard 追加「没有资料」说明；
- ``test_runtime_citations.py``：Runtime 把非法引用写进 trace。

本文件只补这些单测没有连起来的生产路径
``AgentRuntime.prepare → generate → validate_citations``，并钉住四件事：

1. ``SUCCESS_BUT_DROPPED`` 确实进入了最终 system instruction；
2. 被预算丢弃的 chunk 不算「已注入来源」，只有实际注入的 chunk 可被引用；
3. ``validate_citations`` 能把「检索到了但没进上下文」的标签判为非法；
4. 越界引用**不会**被交付：生成阶段的 ``StreamingCitationFilter`` 会把它丢掉，
   同时把「模型原本写了什么」记进 ``CITATION_FILTER`` trace。

第 1—4 点都已由生产代码满足。仍然开放的只有「模型用自然语言误述检索状态」
（例如被裁剪时仍说「没有检索到资料」）：这是语义判断，Runtime 无法在不做
关键词拦截的前提下可靠识别，因此保留一条 ``xfail(strict=True)`` 记录风险，
不把它当作已修复。若将来引入了可靠的语义约束，这条会 XPASS 并提示移除标记。
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from app.citations import provided_labels_from_context, validate_citations
from app.config import Settings
from app.context.builder import ContextBuilder
from app.llm.base import ChatMessage
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
)
from app.runtime import AgentRuntime, PreparedRun
from app.schemas import ChatRequest, Concept, KnowledgeChunk
from app.trace import TraceEventType

QUESTION = "ORBIT-4721 新部署在失败后等待多久再重试？"

INJECTED_LABEL = "资料《orbit_log_format_p5.md》#0"
DROPPED_LABEL = "资料《orbit_current_p5.md》#0"
DROPPED_CITATION = f"[{DROPPED_LABEL}]"

MODEL_OUTPUT = (
    "本轮没有检索到任何相关资料，因此无法确认 ORBIT-4721 的重试窗口。"
    f"{DROPPED_CITATION}"
)


def _injected_chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        id=702,
        document_id=702,
        chunk_index=0,
        document_name="orbit_log_format_p5.md",
        content=(
            "ORBIT-4721 的日志行格式为时间戳、级别、实例与消息。"
            "该片段只讲日志格式，不含重试窗口。"
        ),
    )


def _dropped_chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        id=701,
        document_id=701,
        chunk_index=0,
        document_name="orbit_current_p5.md",
        content=(
            "ORBIT-4721 当前调度规范。当前调度器使用 17 秒的重试窗口，"
            "该值适用于所有新部署。"
        )
        * 120,
    )


class FakeLLM:
    name = "scripted"

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield self.answer

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self.answer

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    name = "scripted-retriever"

    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self.chunks = chunks

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return []

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ):
        return EvidenceRetrievalResult()

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(
            chunks=self.chunks,
            status=KnowledgeStatus.SUCCESS,
            warning=None,
            candidates=len(self.chunks),
        )

    async def aclose(self) -> None:
        return None


def _request() -> ChatRequest:
    return ChatRequest(
        runId=5200,
        userId=1,
        conversationId=5200,
        learningSpaceId=1,
        userMessage=QUESTION,
    )


async def _prepare(
    chunks: list[KnowledgeChunk], budget: int, answer: str = MODEL_OUTPUT
) -> tuple[AgentRuntime, PreparedRun]:
    runtime = AgentRuntime(
        Settings(
            _env_file=None,
            retriever_mode="fixture",
            max_context_tokens=budget,
        ),
        FakeLLM(answer),
        builder=ContextBuilder(max_context_tokens=budget, recent_history_limit=12),
    )
    prepared = await runtime.prepare(_request(), retriever=FakeRetriever(chunks))
    return runtime, prepared


async def _deliver(runtime: AgentRuntime, prepared: PreparedRun) -> str:
    parts: list[str] = []
    async for delta in runtime.generate(prepared):
        parts.append(delta)
    return "".join(parts)


async def test_success_but_dropped_state_reaches_final_model_context() -> None:
    """检索成功但片段全被丢弃：状态进入 Prompt，检索结果仍在 state 里。"""

    runtime, prepared = await _prepare([_dropped_chunk()], budget=80)
    try:
        assert prepared.context.knowledge_state == "SUCCESS_BUT_DROPPED"
        # 检索层确实拿到了片段；只是最终上下文里没有它。
        assert [chunk.id for chunk in prepared.state.knowledge] == [701]
        assert prepared.context.knowledge_injected_chunk_ids == []
        assert prepared.context.knowledge_dropped_chunk_ids == [701]

        system = prepared.context.messages[0].content
        assert "SUCCESS_BUT_DROPPED" in system
        assert DROPPED_LABEL not in system
        assert provided_labels_from_context(prepared.context, prepared.state.knowledge) == []
    finally:
        await runtime.aclose()


async def test_partial_budget_exposes_only_injected_chunk_as_source() -> None:
    """部分注入：只有实际进入 Prompt 的 chunk 能成为可引用来源。"""

    runtime, prepared = await _prepare(
        [_injected_chunk(), _dropped_chunk()], budget=300
    )
    try:
        assert prepared.context.knowledge_state == "SUCCESS_PARTIAL"
        assert prepared.context.knowledge_injected_chunk_ids == [702]
        assert prepared.context.knowledge_dropped_chunk_ids == [701]
        # 检索层仍持有两个 chunk；只有实际注入的 702 进入可引用来源。
        assert [chunk.id for chunk in prepared.state.knowledge] == [702, 701]
        assert provided_labels_from_context(
            prepared.context, prepared.state.knowledge
        ) == [INJECTED_LABEL]
    finally:
        await runtime.aclose()


async def test_partial_budget_delivers_only_injected_citation() -> None:
    """部分注入时，同一段回答里的合法引用保留、越界引用丢弃。"""

    answer = (
        "日志格式可以确认 "
        f"[{INJECTED_LABEL}]；重试窗口无法确认 {DROPPED_CITATION}。"
    )
    runtime, prepared = await _prepare(
        [_injected_chunk(), _dropped_chunk()], budget=300, answer=answer
    )
    try:
        delivered = await _deliver(runtime, prepared)
    finally:
        await runtime.aclose()

    assert prepared.context.knowledge_state == "SUCCESS_PARTIAL"
    assert f"[{INJECTED_LABEL}]" in delivered
    assert DROPPED_CITATION not in delivered
    # 交付文本的校验由 runtime.stream 负责；这里直接复用校验器确认产物干净。
    labels = provided_labels_from_context(
        prepared.context, prepared.state.knowledge
    )
    assert validate_citations(
        delivered, prepared.state.knowledge, provided_labels=labels
    ).invalid == 0


async def test_budget_states_do_not_trigger_no_material_guard() -> None:
    """全裁剪 / 部分注入不是「没有资料」，Guard 不得追加相反说明。

    这两种状态的正确说明已经由 Context Builder 写进 system instruction；
    运行时兜底只处理 NO_MATCH 等「确实没有可用资料」的状态。
    """

    cases = (
        ([_dropped_chunk()], 80, "SUCCESS_BUT_DROPPED"),
        ([_injected_chunk(), _dropped_chunk()], 300, "SUCCESS_PARTIAL"),
    )
    for chunks, budget, expected_state in cases:
        runtime, prepared = await _prepare(
            chunks, budget, answer="根据本轮上下文，只能确认日志格式。"
        )
        try:
            delivered = await _deliver(runtime, prepared)
        finally:
            await runtime.aclose()

        assert prepared.context.knowledge_state == expected_state
        assert delivered == "根据本轮上下文，只能确认日志格式。"
        # 没有追加「本轮没有检索到资料」之类的固定说明。
        assert "没有检索到" not in delivered
        assert "没有可用" not in delivered
        assert not any(
            event.type is TraceEventType.EVIDENCE_GUARD
            for event in prepared.trace.events
        )


async def test_validate_citations_rejects_dropped_chunk_as_source() -> None:
    """被预算丢弃的 chunk 仍存在于检索结果里，但校验器必须判它非法。

    这里直接校验**模型原始输出**，不经过生成阶段的流式过滤：过滤会先把
    越界标注丢掉，交付文本自然不再含非法引用；校验器本身的判定仍然必须
    保留，供离线评估与 trace 使用。
    """

    runtime, prepared = await _prepare(
        [_injected_chunk(), _dropped_chunk()], budget=300
    )
    try:
        labels = provided_labels_from_context(
            prepared.context, prepared.state.knowledge
        )
        report = validate_citations(
            MODEL_OUTPUT, prepared.state.knowledge, provided_labels=labels
        )
        assert report.invalid == 1
        assert report.invalid_labels == [DROPPED_CITATION]
        assert report.valid == 0
    finally:
        await runtime.aclose()


async def test_runtime_must_not_deliver_dropped_chunk_citation() -> None:
    """越界引用不得交付用户：生成阶段就过滤，而不是事后只写 trace。"""

    runtime, prepared = await _prepare(
        [_injected_chunk(), _dropped_chunk()], budget=300
    )
    try:
        delivered = await _deliver(runtime, prepared)
        assert DROPPED_CITATION not in delivered
        # 交付文本本身已经是干净的：事后校验不会再发现非法引用。
        assert runtime.validate_citations(prepared).invalid == 0
        # 但「模型原本写了什么」仍要留在 trace 里，不能因为清洗而消失。
        filtered = [
            event
            for event in prepared.trace.events
            if event.type is TraceEventType.CITATION_FILTER
        ]
        assert len(filtered) == 1
        assert filtered[0].detail["droppedLabels"] == [DROPPED_CITATION]
    finally:
        await runtime.aclose()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "已记录的剩余风险，不是待修复缺陷：检索成功但被裁剪时，模型可能用自然语言"
        "误述「没有检索到资料」。该句是语义断言，确定性状态与引用 ID 无法验证；"
        "按契约不允许用关键词拦截或硬编码改写，因此 Runtime 不做纠正。"
    ),
)
async def test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped() -> None:
    """契约边界：检索成功但被裁掉时，自然语言误述无法被确定性拦截。

    Prompt 里已经写明 ``SUCCESS_BUT_DROPPED`` 与「检索到了但未注入」；
    但模型仍然可能输出「没有检索到任何相关资料」。Runtime 能看到的是
    确定的检索/注入状态，却无法把任意自然语言断言映射回该状态，除非做
    关键词匹配——那正是本轮禁止的通过测试方式。因此该行为以 xfail 记录为
    已知风险，而不是伪装成已修复。
    """

    runtime, prepared = await _prepare([_dropped_chunk()], budget=80)
    try:
        delivered = await _deliver(runtime, prepared)
        assert "没有检索到任何相关资料" not in delivered
    finally:
        await runtime.aclose()
