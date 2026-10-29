"""流式来源过滤的确定性契约。

`StreamingCitationFilter` 是「非法来源不得交付」的唯一执行点，因此这里只断言
可机器核对的行为：

- 合法标注原样交付；
- 越界 / 残缺标注不交付，但保留在 report 里；
- 跨 delta 的标注必须拼完再判定，不能靠单块字符串匹配；
- 普通方括号（Markdown 链接、代码索引）不受影响；
- 合法集合是**每次请求**新建的，不跨请求复用。

「模型用自然语言误述检索状态」不在这里断言：那是语义问题，见
``test_runtime_budget_contract.py`` 里保留的 xfail。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.citation_filter import StreamingCitationFilter, filter_text
from app.config import Settings
from app.llm.base import ChatMessage
from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.runtime import AgentRuntime
from app.schemas import ChatRequest, Concept, KnowledgeChunk

ALLOWED = "资料《orbit_log_format_p5.md》#0"
OTHER = "资料《orbit_current_p5.md》#0"
ALLOWED_CITATION = f"[{ALLOWED}]"
OTHER_CITATION = f"[{OTHER}]"


def _injected_chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        id=702,
        document_id=702,
        chunk_index=0,
        document_name="orbit_log_format_p5.md",
        content="ORBIT-4721 的日志行格式为时间戳、级别、实例与消息。",
    )


def _dropped_chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        id=701,
        document_id=701,
        chunk_index=0,
        document_name="orbit_current_p5.md",
        content="ORBIT-4721 当前调度器使用 17 秒的重试窗口。",
    )


class ChunkedLLM:
    """按给定分段输出，用来验证跨 delta 的标注处理。"""

    name = "chunked"

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        for chunk in self.chunks:
            yield chunk

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self.chunks)

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    name = "scripted-retriever"

    def __init__(self, chunks: list[KnowledgeChunk], status: KnowledgeStatus) -> None:
        self.chunks = chunks
        self.status = status

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return []

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_evidences(self, user_id: int, concept_ids: list[int], limit: int):
        return []

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(
            chunks=self.chunks,
            status=self.status,
            warning=None,
            candidates=len(self.chunks),
        )

    async def aclose(self) -> None:
        return None


def _request(run_id: int = 9000) -> ChatRequest:
    return ChatRequest(
        runId=run_id,
        userId=1,
        conversationId=run_id,
        learningSpaceId=1,
        userMessage="ORBIT-4721 新部署在失败后等待多久再重试？",
    )


async def _deliver(
    runtime: AgentRuntime, prepared
) -> tuple[str, list[str]]:
    """返回 (拼接后的交付文本, SSE 增量列表)。"""
    deltas: list[str] = []
    async for delta in runtime.generate(prepared):
        deltas.append(delta)
    return "".join(deltas), deltas


# --------------------------------------------------------------------------
# 纯过滤器
# --------------------------------------------------------------------------


def test_valid_citation_passes_through() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    out = citation_filter.feed(f"答案是 17 秒 {ALLOWED_CITATION}。")
    out += citation_filter.finish()
    assert out == f"答案是 17 秒 {ALLOWED_CITATION}。"
    assert citation_filter.report.dropped == []
    assert citation_filter.report.malformed == []


def test_out_of_scope_citation_is_dropped_but_reported() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    out = citation_filter.feed(f"答案是 17 秒 {OTHER_CITATION}。")
    out += citation_filter.finish()
    assert OTHER_CITATION not in out
    assert citation_filter.report.dropped == [OTHER_CITATION]
    assert citation_filter.report.provided_labels == [ALLOWED]


def test_citation_split_across_deltas_is_reassembled() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    pieces = ["答案是 17 秒 [资料《orbit_log", "_format_p5.md》#0]。"]
    out = "".join(citation_filter.feed(piece) for piece in pieces)
    out += citation_filter.finish()
    assert out == f"答案是 17 秒 {ALLOWED_CITATION}。"
    assert citation_filter.report.dropped == []


def test_out_of_scope_citation_split_across_deltas_is_dropped() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    pieces = ["答案是 17 秒 [资料《orbit", "_current_p5.md》#0]。"]
    out = "".join(citation_filter.feed(piece) for piece in pieces)
    out += citation_filter.finish()
    assert OTHER_CITATION not in out
    assert citation_filter.report.dropped == [OTHER_CITATION]


def test_malformed_citation_like_is_dropped() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    out = citation_filter.feed("见 [#1] 与 [资料《x.md》]。")
    out += citation_filter.finish()
    assert "#1" not in out
    assert "资料" not in out
    assert citation_filter.report.malformed == ["[#1]", "[资料《x.md》]"]


def test_plain_brackets_are_not_citations() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    text = "参考 [文档](https://example.com) 与数组 a[0]。"
    out = citation_filter.feed(text)
    out += citation_filter.finish()
    assert out == text
    assert citation_filter.report.removed == 0


def test_unclosed_citation_prefix_is_not_delivered() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    out = citation_filter.feed("答案 [资料《orbit_log")
    out += citation_filter.finish()
    assert "资料" not in out
    assert citation_filter.report.malformed == ["[资料《orbit_log"]


def test_incomplete_short_bracket_at_stream_end_is_delivered() -> None:
    citation_filter = StreamingCitationFilter([ALLOWED])
    out = citation_filter.feed("数组下标 [0")
    out += citation_filter.finish()
    assert out == "数组下标 [0"


def test_filter_is_per_request_no_cross_request_reuse() -> None:
    first = StreamingCitationFilter([ALLOWED])
    second = StreamingCitationFilter([OTHER])

    first_out = first.feed(OTHER_CITATION) + first.finish()
    second_out = second.feed(OTHER_CITATION) + second.finish()

    assert first_out == ""
    assert first.report.dropped == [OTHER_CITATION]
    assert second_out == OTHER_CITATION
    assert second.report.dropped == []


def test_filter_text_matches_streaming_behaviour() -> None:
    text = f"结论 {OTHER_CITATION} 与 {ALLOWED_CITATION}。"
    cleaned, report = filter_text(text, [ALLOWED])
    assert cleaned == f"结论  与 {ALLOWED_CITATION}。"
    assert report.dropped == [OTHER_CITATION]


# --------------------------------------------------------------------------
# Runtime 集成
# --------------------------------------------------------------------------


async def test_runtime_delivers_valid_citation_from_injected_source() -> None:
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        ChunkedLLM(["重试窗口是 17 秒 ", ALLOWED_CITATION, "。"]),
    )
    prepared = await runtime.prepare(
        _request(), retriever=FakeRetriever([_injected_chunk()], KnowledgeStatus.SUCCESS)
    )
    try:
        delivered, deltas = await _deliver(runtime, prepared)
    finally:
        await runtime.aclose()

    assert delivered == f"重试窗口是 17 秒 {ALLOWED_CITATION}。"
    # 标注被拆成独立 delta 时，过滤不能把它拆散或延迟到流结束才交付。
    assert any(ALLOWED_CITATION in delta for delta in deltas)
    assert runtime.validate_citations(prepared).valid == 1


async def test_runtime_drops_citation_split_across_deltas() -> None:
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        ChunkedLLM(["重试窗口是 ", "17", " 秒 [资料《orbit", "_current_p5.md》#0]。"]),
    )
    prepared = await runtime.prepare(
        _request(), retriever=FakeRetriever([_injected_chunk()], KnowledgeStatus.SUCCESS)
    )
    try:
        delivered, _ = await _deliver(runtime, prepared)
    finally:
        await runtime.aclose()

    assert "orbit_current" not in delivered
    assert delivered == "重试窗口是 17 秒 。"
    assert runtime.validate_citations(prepared).invalid == 0


async def test_runtime_does_not_reuse_source_set_between_requests() -> None:
    """同一条 runtime：第一次有注入来源，第二次检索为空，集合不得复用。"""

    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        ChunkedLLM([f"重试窗口是 17 秒 {OTHER_CITATION}。"]),
    )
    first = await runtime.prepare(
        _request(9001),
        retriever=FakeRetriever(
            [_injected_chunk(), _dropped_chunk()], KnowledgeStatus.SUCCESS
        ),
    )
    second = await runtime.prepare(
        _request(9002),
        retriever=FakeRetriever([], KnowledgeStatus.NO_MATCH),
    )
    try:
        first_delivered, _ = await _deliver(runtime, first)
        second_delivered, _ = await _deliver(runtime, second)
    finally:
        await runtime.aclose()

    # 第一次里 701 实际进入了上下文（预算足够），引用合法。
    assert OTHER_CITATION in first_delivered
    assert runtime.validate_citations(first).valid == 1

    # 第二次没有检索结果，同一个标签必须被丢弃，不能沿用第一次的集合。
    assert OTHER_CITATION not in second_delivered
    assert runtime.validate_citations(second).invalid == 0


async def test_runtime_with_no_retrieval_drops_citation() -> None:
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        ChunkedLLM([f"没有资料 {ALLOWED_CITATION}。"]),
    )
    prepared = await runtime.prepare(
        _request(), retriever=FakeRetriever([], KnowledgeStatus.NO_MATCH)
    )
    try:
        delivered, _ = await _deliver(runtime, prepared)
    finally:
        await runtime.aclose()

    assert ALLOWED_CITATION not in delivered
    assert runtime.validate_citations(prepared).invalid == 0
