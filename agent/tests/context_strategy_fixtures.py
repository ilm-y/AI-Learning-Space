"""A/B/C 上下文策略测试的确定性固定装置。

这些装置不连 MySQL / Qdrant / 真实模型：

- ``DeterministicRetriever`` 返回固定顺序、固定内容的检索结果；
- ``FakeLLM`` 返回固定回答，其中含一条可校验的文档引用；
- ``collect_payload()`` 走生产 ``prepare → generate → validate_citations`` 链路，
  把 Manifest、引用、状态与 trace 收敛成可直接做 golden 对比的 JSON。

C 策略的 golden 在实现策略开关**之前**生成，开关后必须逐字节一致
（见 ``test_context_strategy.py``），用于证明默认路径没有行为变化。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from app.citations import provided_labels_from_context, structured_citations, validate_citations
from app.config import Settings
from app.llm.base import ChatMessage
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
    MemoryRetrievalResult,
    RelatedConceptResult,
    RelatedConceptStatus,
    RetrievalStatus,
    RevisionRetrievalResult,
    UnderstandingRetrievalResult,
)
from app.runtime import AgentRuntime
from app.schemas import (
    ChatRequest,
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceType,
    HistoryMessage,
    KnowledgeChunk,
    MemoryItem,
    MemoryScopeType,
    MemoryType,
    MessageRole,
    SessionContext,
    Understanding,
    UnderstandingRevision,
    UnderstandingStatus,
)
from app.trace import RunTrace

# Pilot stamp 的形状；与 ``agent/app/config.py`` 的 test_env_id 正则一致。
ENV_ID = "phase5-pilot-20261109120000-abcdef"

QUESTION = "请解释 TCP 三次握手中第三次握手的作用。"

# 回答里引用的标签必须与固定 chunk 的 document_name / chunk_index 一致。
RESPONSE = (
    "第三次握手确认服务端的 SYN-ACK 已被客户端收到，"
    "避免旧连接请求被服务端误当成新连接。[资料《tcp_handshake.md》#0]"
)

CONCEPT = Concept(
    id=1,
    canonical_name="TCP 三次握手",
    display_name="TCP 三次握手",
)

HISTORY = [
    HistoryMessage(role=MessageRole.USER, content="SYN 和 SYN-ACK 分别是谁发的？"),
    HistoryMessage(
        role=MessageRole.ASSISTANT,
        content="客户端发 SYN，服务端回 SYN-ACK，客户端最后回 ACK。",
    ),
]

SESSION = SessionContext(
    id=5,
    title="TCP 握手",
    goal="能完整解释三次握手每一步的必要性",
    current_focus="第三次握手是否只是确认服务端收到 ACK",
)

UNDERSTANDING = Understanding(
    id=31,
    concept_id=1,
    concept_name="TCP 三次握手",
    summary="知道三步顺序，但不确定第三次握手的必要性。",
    known_parts=["能说出 SYN、SYN-ACK、ACK 的顺序"],
    uncertain_parts=["第三次握手是否只是为了确认服务端收到 ACK"],
    latest_user_explanation="客户端先发 SYN，服务端回 SYN-ACK，客户端再回 ACK。",
    status=UnderstandingStatus.PARTIAL,
    current_revision_id=41,
)

MEMORY = MemoryItem(
    id=51,
    scope_type=MemoryScopeType.CONCEPT,
    learning_space_id=9,
    concept_id=1,
    memory_type=MemoryType.LEARNING,
    memory_key="tcp-handshake-style",
    content="用户偏好先用报文顺序理解三次握手，再补协议细节。",
    importance=0.9,
)

EVIDENCE = EvidenceItem(
    id=61,
    concept_id=1,
    understanding_id=31,
    evidence_type=EvidenceType.USER_EXPLANATION,
    observation="用户能按顺序复述三次握手，但没有解释第三次握手的必要性。",
    correctness=EvidenceCorrectness.PARTIAL,
)

REVISION = UnderstandingRevision(
    id=41,
    concept_id=1,
    revision_no=1,
    summary="知道三步顺序，但不确定第三次握手的必要性。",
    known_parts=["能说出 SYN、SYN-ACK、ACK 的顺序"],
    uncertain_parts=["第三次握手是否只是为了确认服务端收到 ACK"],
    user_explanation="客户端先发 SYN，服务端回 SYN-ACK，客户端再回 ACK。",
    status=UnderstandingStatus.PARTIAL,
    trigger_evidence_id=61,
)

CHUNK = KnowledgeChunk(
    id=701,
    document_id=801,
    chunk_index=0,
    document_name="tcp_handshake.md",
    file_type="text/markdown",
    content=(
        "第三次握手（ACK）用于确认服务端此前发出的 SYN-ACK 已被客户端收到，"
        "使服务端确认客户端的接收与发送能力都正常，并避免历史重复连接请求"
        "被误认为新连接。"
    ),
    score=0.91,
)


class FakeLLM:
    """固定输出；不模拟 provider usage（C golden 只覆盖上下文与引用路径）。"""

    name = "fake"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield RESPONSE

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return RESPONSE

    async def aclose(self) -> None:
        return None


class DeterministicRetriever:
    """记录每个数据源是否被调用，并按固定内容返回结果。"""

    name = "deterministic"

    def __init__(
        self,
        *,
        degraded: bool = False,
        concepts: list[Concept] | None = None,
    ) -> None:
        self.degraded = degraded
        self.concepts = list(concepts or [CONCEPT])
        self.calls: list[str] = []

    async def list_concepts(self, space_id: int) -> list[Concept]:
        self.calls.append("concepts")
        return list(self.concepts)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> UnderstandingRetrievalResult:
        self.calls.append("understandings")
        return UnderstandingRetrievalResult(
            items=[UNDERSTANDING],
            status=RetrievalStatus.DEGRADED if self.degraded else RetrievalStatus.SUCCESS,
            warning="认知检索失败" if self.degraded else None,
        )

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> MemoryRetrievalResult:
        self.calls.append("memories")
        return MemoryRetrievalResult(
            items=[MEMORY],
            status=RetrievalStatus.DEGRADED if self.degraded else RetrievalStatus.SUCCESS,
            warning="记忆检索失败" if self.degraded else None,
        )

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        self.calls.append("knowledge")
        return KnowledgeResult(
            chunks=[CHUNK],
            status=KnowledgeStatus.SUCCESS,
            candidates=1,
        )

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        self.calls.append("evidences")
        if self.degraded:
            return EvidenceRetrievalResult.degraded(
                "证据检索失败", items=[EVIDENCE]
            )
        return EvidenceRetrievalResult.from_items([EVIDENCE])

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        self.calls.append("related")
        return RelatedConceptResult(status=RelatedConceptStatus.EMPTY)

    async def get_revisions(
        self,
        user_id: int,
        concept_ids: list[int],
        *,
        space_id: int | None = None,
    ) -> RevisionRetrievalResult:
        self.calls.append("revisions")
        return RevisionRetrievalResult(
            revisions={1: [REVISION]},
            status=RetrievalStatus.DEGRADED if self.degraded else RetrievalStatus.SUCCESS,
            warning="版本检索失败" if self.degraded else None,
        )

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ) -> list[EvidenceItem]:
        self.calls.append("evidence_details")
        return [EVIDENCE]

    async def aclose(self) -> None:
        return None


def make_settings(strategy: str = "C") -> Settings:
    """构造 Settings；非 C 策略自动带上满足安全约束的隔离测试环境。"""
    overrides: dict[str, object] = {}
    if strategy != "C":
        overrides = {
            "test_only": True,
            "test_env_id": ENV_ID,
            "qdrant_collection_prefix": ENV_ID,
            "memory_collection_prefix": f"{ENV_ID}-memory",
            "host": "127.0.0.1",
        }
    return Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        max_context_tokens=3000,
        recent_history_limit=12,
        context_strategy=strategy,
        **overrides,
    )


def make_request() -> ChatRequest:
    return ChatRequest(
        runId=77,
        traceId="trace-context-strategy",
        userId=7,
        conversationId=70,
        learningSpaceId=9,
        userMessage=QUESTION,
        history=list(HISTORY),
        session=SESSION,
    )


async def collect_payload(
    strategy: str,
    *,
    retriever: DeterministicRetriever | None = None,
    request: ChatRequest | None = None,
) -> dict[str, object]:
    """走生产 prepare/generate/validate 链路，返回可做 golden 对比的字段。

    刻意不包含 ``elapsedMs`` / ``startedAt`` 等时间字段：它们不稳定，也不属于
    行为契约。Usage 由 provider 适配层产生，本批未改动该层；这里记录终态字段
    以证明 generate 的终态契约没有变化。
    """
    retriever = retriever or DeterministicRetriever()
    runtime = AgentRuntime(make_settings(strategy), FakeLLM())
    trace = RunTrace(run_id=77, trace_id="trace-context-strategy")
    prepared = await runtime.prepare(
        request or make_request(), trace, retriever=retriever
    )

    response = "".join([delta async for delta in runtime.generate(prepared)])
    report = validate_citations(
        response,
        prepared.state.knowledge,
        provided_labels=provided_labels_from_context(
            prepared.context, prepared.state.knowledge
        ),
    )

    return {
        "strategy": strategy,
        "retrieverCalls": list(retriever.calls),
        "manifest": prepared.context.context_manifest,
        "sectionNames": [section.name for section in prepared.context.sections],
        "sectionTypes": [section.source_type for section in prepared.context.sections],
        "knowledgeState": prepared.context.knowledge_state,
        "estimatedTokens": prepared.context.estimated_tokens,
        "contextTokens": prepared.context.context_tokens,
        "providedLabels": provided_labels_from_context(
            prepared.context, prepared.state.knowledge
        ),
        "citations": structured_citations(report),
        "citationInvalidLabels": list(report.invalid_labels),
        "response": response,
        "retrievalPlan": (
            prepared.state.retrieval_plan.as_dict()
            if prepared.state.retrieval_plan is not None
            else None
        ),
        "statuses": {
            "understanding": prepared.state.understanding_status,
            "memory": prepared.state.memory_status,
            "evidence": prepared.state.evidence_status,
            "revision": prepared.state.revision_status,
            "knowledge": prepared.state.knowledge_status,
        },
        "historyMessages": len(prepared.state.history),
        "sessionPresent": prepared.state.session is not None,
        "terminal": {
            "finishReason": None,
            "truncated": False,
            "usage": None,
            "usageStatus": "UNKNOWN",
        },
        "traceEvents": [
            {"type": event.type.value, "detail": event.detail}
            for event in trace.events
        ],
    }


def payload_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
