"""Runtime 的引用校验集成测试：不联网，验证 trace 告警真的接上了。"""

from __future__ import annotations

from typing import AsyncIterator

from app.config import Settings
from app.llm.base import ChatMessage
from app.retriever.base import KnowledgeResult, KnowledgeStatus
from app.runtime import AgentRuntime
from app.schemas import (
    ChatRequest,
    Concept,
    KnowledgeChunk,
)
from app.trace import TraceEventType


class FakeLLM:
    name = "fake"

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
    name = "fake-retriever"

    def __init__(self, chunk: KnowledgeChunk | None) -> None:
        self.chunk = chunk

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return []

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_knowledge(self, query) -> KnowledgeResult:
        if self.chunk is None:
            return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)
        return KnowledgeResult(chunks=[self.chunk], status=KnowledgeStatus.SUCCESS)

    async def get_evidences(self, user_id: int, concept_ids: list[int], limit: int):
        return []

    async def aclose(self) -> None:
        return None


def request(question: str) -> ChatRequest:
    return ChatRequest(
        runId=1,
        userId=1,
        conversationId=1,
        learningSpaceId=1,
        userMessage=question,
    )


async def collect_answer(runtime: AgentRuntime, prepared) -> str:
    parts = []
    async for delta in runtime.generate(prepared):
        parts.append(delta)
    return "".join(parts)


async def test_runtime_records_invalid_citation_in_trace() -> None:
    chunk = KnowledgeChunk(
        id=1,
        document_id=10,
        chunk_index=0,
        document_name="zenframe.md",
        content="ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。",
    )
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        FakeLLM("ZenFrame 使用 MQTT [资料《不存在.md》#9]。"),
    )
    prepared = await runtime.prepare(
        request("ZenFrame 的提醒模块通过什么协议？"),
        retriever=FakeRetriever(chunk),
    )
    try:
        await collect_answer(runtime, prepared)
        report = runtime.validate_citations(prepared)
    finally:
        await runtime.aclose()

    assert report.invalid == 1
    assert report.invalid_labels == ["[资料《不存在.md》#9]"]
    citation_events = [
        event
        for event in prepared.trace.events
        if event.type is TraceEventType.CITATION_CHECK
    ]
    assert len(citation_events) == 1
    assert citation_events[0].detail["invalid"] == 1
    assert citation_events[0].detail["invalidLabels"] == ["[资料《不存在.md》#9]"]


async def test_runtime_accepts_valid_citation() -> None:
    chunk = KnowledgeChunk(
        id=1,
        document_id=10,
        chunk_index=0,
        document_name="zenframe.md",
        content="ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。",
    )
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        FakeLLM("ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒 [资料《zenframe.md》#0]。"),
    )
    prepared = await runtime.prepare(
        request("ZenFrame 的提醒模块通过什么协议？"),
        retriever=FakeRetriever(chunk),
    )
    try:
        await collect_answer(runtime, prepared)
        report = runtime.validate_citations(prepared)
    finally:
        await runtime.aclose()

    assert report.valid == 1
    assert report.invalid == 0
