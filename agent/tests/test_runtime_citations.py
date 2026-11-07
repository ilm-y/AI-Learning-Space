"""Runtime 的引用校验集成测试：不联网，验证 trace 告警真的接上了。"""

from __future__ import annotations

from typing import AsyncIterator

from app.citations import structured_citations, validate_citations
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


async def test_runtime_filters_invalid_citation_and_records_it_in_trace() -> None:
    """非法来源在交付前被丢弃，原样内容留在 CITATION_FILTER 里。"""

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
        answer = await collect_answer(runtime, prepared)
        report = runtime.validate_citations(prepared)
    finally:
        await runtime.aclose()

    # 交付给用户的文本里没有越界来源。标注前的空格在识别出 `[` 之前就已经
    # 放行，流式无法撤回，因此交付文本保留一个空格；这是延迟取舍，不是引用泄漏。
    assert "不存在.md" not in answer
    assert answer == "ZenFrame 使用 MQTT 。"

    # 交付文本本身的校验结果干净；模型的原始错误在过滤器 trace 里。
    assert report.invalid == 0
    filter_events = [
        event
        for event in prepared.trace.events
        if event.type is TraceEventType.CITATION_FILTER
    ]
    assert len(filter_events) == 1
    assert filter_events[0].detail["droppedLabels"] == ["[资料《不存在.md》#9]"]
    assert filter_events[0].detail["malformedLabels"] == []
    assert filter_events[0].detail["providedLabels"] == ["资料《zenframe.md》#0"]

    citation_events = [
        event
        for event in prepared.trace.events
        if event.type is TraceEventType.CITATION_CHECK
    ]
    assert len(citation_events) == 1
    assert citation_events[0].detail["invalid"] == 0


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


def _chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        id=1,
        document_id=10,
        chunk_index=0,
        document_name="zenframe.md",
        content="ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。",
    )


async def test_stream_done_carries_structured_citations(monkeypatch) -> None:
    """终态事件必须携带结构化来源，客户端不再只能解析正文标签。"""

    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        FakeLLM(
            "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒 [资料《zenframe.md》#0]。"
        ),
    )
    monkeypatch.setattr(
        "app.runtime.build_retriever", lambda *args, **kwargs: FakeRetriever(_chunk())
    )
    try:
        events = [
            event
            async for event in runtime.stream(
                request("ZenFrame 的提醒模块通过什么协议？")
            )
        ]
    finally:
        await runtime.aclose()

    done = events[-1]
    assert done.event == "done"
    assert done.data["citations"] == [
        {
            "documentId": 10,
            "chunkId": 1,
            "chunkIndex": 0,
            "sourceTitle": "zenframe.md",
        }
    ]


async def test_stream_done_citations_is_empty_without_labels(monkeypatch) -> None:
    """没有引用时下发空数组，保持旧客户端可忽略该字段。"""

    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        FakeLLM("ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。"),
    )
    monkeypatch.setattr(
        "app.runtime.build_retriever", lambda *args, **kwargs: FakeRetriever(_chunk())
    )
    try:
        events = [
            event
            async for event in runtime.stream(
                request("ZenFrame 的提醒模块通过什么协议？")
            )
        ]
    finally:
        await runtime.aclose()

    assert events[-1].event == "done"
    assert events[-1].data["citations"] == []


def test_structured_citations_deduplicate_and_skip_unresolvable_labels() -> None:
    hint = _chunk()
    report = validate_citations(
        "第一句 [资料《zenframe.md》#0]。第二句再次引用 [资料《zenframe.md》#0]，"
        "还有一条不存在的 [资料《missing.md》#9]。",
        [hint],
    )

    assert report.valid == 2
    assert report.invalid == 1
    assert structured_citations(report) == [
        {
            "documentId": 10,
            "chunkId": 1,
            "chunkIndex": 0,
            "sourceTitle": "zenframe.md",
        }
    ]
