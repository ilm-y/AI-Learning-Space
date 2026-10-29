"""流式回答截断的运行时契约。

背景：适配器早已在 ``StreamDiagnostics.finish_reason`` 里记录了供应商的完成原因，
但 ``stream_chat`` 只向上层产出字符串，Runtime 因此把 ``finish_reason=length``
也当成正常完成，最终 SSE ``done`` 无法区分「讲完了」和「被 Token 上限截断」。

本文件钉住四件事：

1. 正常结束仍然产出 ``done``，``truncated=false``；
2. 部分 delta 后遇到 ``length``，文本保留、``done` 明确标记 ``truncated=true``；
3. 模型调用失败仍走 ``error``，不会被终态元数据掩盖；
4. 空返回仍按既有约定报错，不产生一条空回答。

测试使用可控固定适配器，不依赖真实模型；真实适配器的解析另见
``test_llm_openai_compatible.py``。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.config import Settings
from app.llm.base import (
    ChatMessage,
    CompletionAwareStream,
    LLMError,
    StreamCompletion,
)
from app.runtime import AgentRuntime
from tests.helpers import make_request


class _CompletionLLM:
    """可指定增量文本、终态和失败点的固定适配器。"""

    name = "completion-scripted"

    def __init__(
        self,
        deltas: list[str],
        finish_reason: str | None = "stop",
        *,
        error: LLMError | None = None,
        expose_metadata: bool = True,
    ) -> None:
        self._deltas = deltas
        self._finish_reason = finish_reason
        self._error = error
        self._expose_metadata = expose_metadata
        self.calls = 0

    async def _generate(self) -> AsyncIterator[str]:
        for delta in self._deltas:
            yield delta
        if self._error is not None:
            raise self._error

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        async for delta in self._generate():
            yield delta

    def stream_chat_with_completion(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionAwareStream:
        self.calls += 1
        return CompletionAwareStream(
            self._generate(),
            lambda: StreamCompletion(
                finish_reason=self._finish_reason,
                metadata_available=self._expose_metadata,
            ),
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self._deltas)

    async def aclose(self) -> None:
        return None


class _LegacyLLM:
    """只实现旧 ``stream_chat`` 的适配器：终态未知，必须按正常完成兼容。"""

    name = "legacy"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "旧适配器的回答"

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return "旧适配器的回答"

    async def aclose(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        retriever_mode="fixture",
        max_context_tokens=3000,
    )


async def _collect(llm) -> list:
    runtime = AgentRuntime(_settings(), llm)
    try:
        events = [
            event
            async for event in runtime.stream(make_request("TLB 是什么？"))
        ]
    finally:
        await runtime.aclose()
    return events


def _joined(events: list) -> str:
    return "".join(
        str(event.data["delta"]) for event in events if event.event == "delta"
    )


async def test_normal_completion_stays_completed_and_untruncated() -> None:
    events = await _collect(_CompletionLLM(["TLB ", "是页表项缓存。"], "stop"))

    assert events[0].event == "meta"
    assert events[-1].event == "done"
    assert _joined(events) == "TLB 是页表项缓存。"
    assert events[-1].data["finishReason"] == "stop"
    assert events[-1].data["truncated"] is False
    assert not any(event.event == "error" for event in events)


async def test_partial_answer_before_length_is_kept_but_marked_truncated() -> None:
    events = await _collect(_CompletionLLM(["TLB 缓存的是页表项，"], "length"))

    assert events[-1].event == "done"
    # 已经产出的部分内容仍然交给用户，不从流里删掉
    assert _joined(events) == "TLB 缓存的是页表项，"
    # 但终态必须是「截断」，不能伪装成正常完成
    assert events[-1].data["finishReason"] == "length"
    assert events[-1].data["truncated"] is True


async def test_model_failure_still_uses_error_path() -> None:
    events = await _collect(
        _CompletionLLM(["半截"], "length", error=LLMError("模型连接中断"))
    )

    assert events[-1].event == "error"
    assert events[-1].data["code"] == 502
    assert "模型连接中断" in str(events[-1].data["message"])
    assert not any(event.event == "done" for event in events)


async def test_empty_output_still_becomes_explicit_error() -> None:
    events = await _collect(_CompletionLLM([], "stop"))

    assert events[-1].event == "error"
    assert events[-1].data["code"] == 502
    assert "没有返回任何内容" in str(events[-1].data["message"])
    assert not any(event.event == "done" for event in events)


async def test_adapter_without_completion_metadata_keeps_legacy_behavior() -> None:
    events = await _collect(_LegacyLLM())

    assert events[-1].event == "done"
    assert events[-1].data["finishReason"] is None
    assert events[-1].data["truncated"] is False
