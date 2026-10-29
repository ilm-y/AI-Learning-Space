"""OpenAI 兼容适配器。

DeepSeek / OpenAI / 通义千问 / vLLM / Ollama 都提供 `/chat/completions`，
差别只在 base_url、api_key、model 三个配置项，所以这里只写一个实现。

流式解析的是标准 SSE：逐行读 `data: {...}`，遇到 `data: [DONE]` 结束。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from app.llm.base import (
    ChatMessage,
    CompletionAwareStream,
    LLMError,
    StreamCompletion,
)


@dataclass(slots=True)
class StreamDiagnostics:
    """一次 stream_chat 的可观测事实，不包含 prompt、Authorization 或密钥。

    对空流排查最重要的是区分「服务端什么都没发回来」「只发了 reasoning」和
    「HTTP 200 但根本不是 SSE」。仅凭 status=200 无法证明模型完成了生成。
    """

    request_id: str
    model: str
    attempt: int = 0
    max_attempts: int = 1
    http_status: int | None = None
    provider_request_id: str | None = None
    content_type: str | None = None
    data_event_count: int = 0
    empty_choice_count: int = 0
    content_delta_count: int = 0
    reasoning_delta_count: int = 0
    finish_reason: str | None = None
    usage: dict[str, Any] | None = field(default=None)
    saw_done: bool = False
    error_category: str = "none"
    empty_reason: str | None = None
    retryable: bool = False
    latency_ms: int = 0
    content_chars: int = 0
    attempt_history: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "model": self.model,
            "attempt": self.attempt,
            "maxAttempts": self.max_attempts,
            "httpStatus": self.http_status,
            "providerRequestId": self.provider_request_id,
            "contentType": self.content_type,
            "dataEventCount": self.data_event_count,
            "emptyChoiceCount": self.empty_choice_count,
            "contentDeltaCount": self.content_delta_count,
            "reasoningDeltaCount": self.reasoning_delta_count,
            "finishReason": self.finish_reason,
            "usage": dict(self.usage) if self.usage else None,
            "sawDone": self.saw_done,
            "errorCategory": self.error_category,
            "emptyReason": self.empty_reason,
            "retryable": self.retryable,
            "latencyMs": self.latency_ms,
            "contentChars": self.content_chars,
            "attemptHistory": list(self.attempt_history),
        }


@dataclass(slots=True)
class CompletionDiagnostics:
    """一次非流式 ``complete`` 的响应事实，同样不保存内容与凭据。"""

    request_id: str
    model: str
    http_status: int | None = None
    provider_request_id: str | None = None
    content_type: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = field(default=None)
    content_chars: int = 0
    reasoning_chars: int = 0
    error_category: str = "none"
    empty_reason: str | None = None
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "model": self.model,
            "httpStatus": self.http_status,
            "providerRequestId": self.provider_request_id,
            "contentType": self.content_type,
            "finishReason": self.finish_reason,
            "usage": dict(self.usage) if self.usage else None,
            "contentChars": self.content_chars,
            "reasoningChars": self.reasoning_chars,
            "errorCategory": self.error_category,
            "emptyReason": self.empty_reason,
            "latencyMs": self.latency_ms,
        }


class OpenAICompatibleAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.6,
        max_tokens: int = 1024,
        timeout_seconds: float = 60.0,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        # 有界重试只用于「没有产出任何字符」的失败。已经向用户吐过字的流
        # 不能重放，否则会拼接出重复文本。
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        # 仅用于观测：流式响应本身不携带 HTTP 状态，评估需要知道这次是不是 2xx。
        self.last_status_code: int | None = None
        self.last_diagnostics: StreamDiagnostics | None = None
        self.last_completion_diagnostics: CompletionDiagnostics | None = None

    @property
    def name(self) -> str:
        return f"openai-compatible:{self._model}"

    # ---------- 对外能力 ----------

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        async for delta in self.stream_chat_with_completion(
            messages, temperature=temperature, max_tokens=max_tokens
        ):
            yield delta

    def stream_chat_with_completion(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionAwareStream:
        """流式生成，并在正常耗尽后暴露本次的 ``finish_reason``。

        每次调用都创建独立的 ``StreamDiagnostics``，终态由闭包捕获；不依赖
        ``last_diagnostics`` 这类共享字段，因此并发请求不会串号。
        """
        payload = self._payload(messages, temperature, max_tokens, stream=True)
        diagnostics = StreamDiagnostics(
            request_id=uuid.uuid4().hex,
            model=self._model,
            max_attempts=self._max_attempts,
        )
        self.last_diagnostics = diagnostics
        self.last_status_code = None
        return CompletionAwareStream(
            self._stream_chat(payload, diagnostics),
            lambda: StreamCompletion(
                finish_reason=diagnostics.finish_reason,
                metadata_available=True,
            ),
        )

    async def _stream_chat(
        self,
        payload: dict[str, object],
        diagnostics: StreamDiagnostics,
    ) -> AsyncIterator[str]:
        started = time.perf_counter()
        emitted = 0
        try:
            for attempt in range(1, self._max_attempts + 1):
                diagnostics.attempt = attempt
                # HTTP 层与 finish_reason 属于单次尝试；不要让上一次的残留值
                # 混进本次诊断。累计计数留给 attempt_history 逐次拆分。
                diagnostics.http_status = None
                diagnostics.provider_request_id = None
                diagnostics.content_type = None
                diagnostics.finish_reason = None
                diagnostics.usage = None
                diagnostics.saw_done = False
                diagnostics.empty_reason = None
                before = _attempt_snapshot(diagnostics, attempt)
                try:
                    async for delta in self._stream_once(payload, diagnostics):
                        emitted += len(delta)
                        diagnostics.content_chars += len(delta)
                        yield delta
                except LLMError as exc:
                    if diagnostics.error_category == "none":
                        diagnostics.error_category = _category_from_error(exc)
                    before["outcome"] = "error"
                    before["errorCategory"] = diagnostics.error_category
                    _finish_attempt(before, diagnostics)
                    diagnostics.attempt_history.append(before)
                    if (
                        emitted > 0
                        or not diagnostics.retryable
                        or attempt >= self._max_attempts
                    ):
                        raise
                    await asyncio.sleep(self._retry_backoff_seconds * attempt)
                    continue

                if emitted > 0:
                    before["outcome"] = "content"
                    _finish_attempt(before, diagnostics)
                    diagnostics.attempt_history.append(before)
                    diagnostics.error_category = "none"
                    diagnostics.empty_reason = None
                    diagnostics.retryable = False
                    return

                diagnostics.error_category = "empty_stream"
                diagnostics.retryable = True
                before["outcome"] = "empty"
                _finish_attempt(before, diagnostics)
                diagnostics.empty_reason = _empty_reason(diagnostics, before)
                before["emptyReason"] = diagnostics.empty_reason
                diagnostics.attempt_history.append(before)
                if attempt < self._max_attempts:
                    await asyncio.sleep(self._retry_backoff_seconds * attempt)
                    continue

                raise LLMError(
                    "模型返回空流"
                    f"（reason={diagnostics.empty_reason}, "
                    f"status={diagnostics.http_status}, "
                    f"attempts={diagnostics.attempt}）",
                    status_code=502,
                )
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            # 不能让 httpx 的超时冒成未分类异常：Runtime 会把它当成 500「内部错误」，
            # 用户看到的错误码就不再指向「模型上游超时」。
            diagnostics.error_category = "timeout"
            raise LLMError("模型请求超时", status_code=502) from exc
        except httpx.HTTPError as exc:
            diagnostics.error_category = "connection_error"
            raise LLMError(
                f"模型连接失败：{type(exc).__name__}", status_code=502
            ) from exc
        finally:
            diagnostics.latency_ms = int((time.perf_counter() - started) * 1000)

    async def _stream_once(
        self,
        payload: dict[str, object],
        diagnostics: StreamDiagnostics,
    ) -> AsyncIterator[str]:
        async with self._client.stream(
            "POST", self._endpoint, json=payload, headers=self._headers
        ) as response:
            self.last_status_code = diagnostics.http_status = response.status_code
            diagnostics.content_type = response.headers.get("content-type")
            diagnostics.provider_request_id = _provider_request_id(response.headers)
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                diagnostics.error_category = "http_error"
                diagnostics.retryable = response.status_code in (429, 500, 502, 503, 504)
                raise LLMError(
                    f"模型返回 {response.status_code}：{body[:500]}", status_code=502
                )
            async for line in response.aiter_lines():
                kind, chunk = _parse_sse_line(line)
                if kind == "done":
                    diagnostics.saw_done = True
                    continue
                if kind != "data" or chunk is None:
                    continue
                diagnostics.data_event_count += 1
                if chunk.get("error"):
                    diagnostics.error_category = "model_error"
                    raise LLMError(
                        f"模型流式响应包含错误：{_error_text(chunk.get('error'))}",
                        status_code=502,
                    )
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    diagnostics.usage = usage
                choices = chunk.get("choices") or []
                if not choices:
                    diagnostics.empty_choice_count += 1
                    continue
                choice = choices[0] if isinstance(choices[0], dict) else {}
                finish_reason = choice.get("finish_reason")
                if isinstance(finish_reason, str) and finish_reason:
                    diagnostics.finish_reason = finish_reason
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    diagnostics.reasoning_delta_count += 1
                content = delta.get("content")
                if isinstance(content, str) and content:
                    diagnostics.content_delta_count += 1
                    yield content

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        diagnostics = CompletionDiagnostics(
            request_id=uuid.uuid4().hex,
            model=self._model,
        )
        self.last_completion_diagnostics = diagnostics
        self.last_status_code = None
        started = time.perf_counter()
        try:
            try:
                response = await self._client.post(
                    self._endpoint, json=payload, headers=self._headers
                )
            except httpx.TimeoutException as exc:
                diagnostics.error_category = "timeout"
                raise LLMError("模型请求超时", status_code=502) from exc
            except httpx.HTTPError as exc:
                diagnostics.error_category = "connection_error"
                raise LLMError(
                    f"模型连接失败：{type(exc).__name__}", status_code=502
                ) from exc

            self.last_status_code = diagnostics.http_status = response.status_code
            diagnostics.content_type = response.headers.get("content-type")
            diagnostics.provider_request_id = _provider_request_id(response.headers)
            if response.status_code >= 400:
                diagnostics.error_category = "http_error"
                raise LLMError(
                    f"模型返回 {response.status_code}：{response.text[:500]}",
                    status_code=502,
                )
            try:
                body = response.json()
            except ValueError as exc:
                diagnostics.error_category = "format_incompatible"
                raise LLMError(
                    f"模型响应不是合法 JSON：{response.text[:500]}"
                ) from exc
            try:
                if not isinstance(body, dict):
                    raise TypeError("body 不是对象")
                choice = body["choices"][0]
                message = choice["message"]
                content = message["content"]
            except (KeyError, IndexError, TypeError) as exc:
                diagnostics.error_category = "format_incompatible"
                raise LLMError(
                    f"模型响应结构不符合预期：{str(body)[:500]}"
                ) from exc

            if isinstance(choice, dict):
                finish_reason = choice.get("finish_reason")
                if isinstance(finish_reason, str) and finish_reason:
                    diagnostics.finish_reason = finish_reason
            usage = body.get("usage")
            if isinstance(usage, dict):
                diagnostics.usage = usage
            if isinstance(message, dict):
                reasoning = message.get("reasoning_content") or message.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    diagnostics.reasoning_chars = len(reasoning)

            if content is None:
                diagnostics.content_chars = 0
                diagnostics.error_category = "empty_completion"
                diagnostics.empty_reason = _completion_empty_reason(diagnostics)
                raise LLMError(
                    "模型返回空内容"
                    f"（reason={diagnostics.empty_reason}, "
                    f"finish_reason={diagnostics.finish_reason}, "
                    f"status={diagnostics.http_status}）",
                    status_code=502,
                )
            if not isinstance(content, str):
                diagnostics.error_category = "format_incompatible"
                raise LLMError(
                    f"模型响应 content 不是字符串：{type(content).__name__}"
                )
            diagnostics.content_chars = len(content)
            if not content.strip():
                diagnostics.error_category = "empty_completion"
                diagnostics.empty_reason = _completion_empty_reason(diagnostics)
                raise LLMError(
                    "模型返回空内容"
                    f"（reason={diagnostics.empty_reason}, "
                    f"finish_reason={diagnostics.finish_reason}, "
                    f"status={diagnostics.http_status}）",
                    status_code=502,
                )
            return content
        finally:
            diagnostics.latency_ms = int((time.perf_counter() - started) * 1000)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- 内部 ----------

    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _payload(
        self,
        messages: list[ChatMessage],
        temperature: float | None,
        max_tokens: int | None,
        *,
        stream: bool,
    ) -> dict[str, object]:
        return {
            "model": self._model,
            "messages": [message.as_dict() for message in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }

    @staticmethod
    def _parse_chunk(line: str) -> dict | None:
        """把一行 SSE 变成 JSON 对象；不是数据行就返回 None。

        单行坏 JSON 仍然跳过：有些服务会推送 keep-alive 或注释行，不能因为
        一行解析失败就中断整段回答。但如果一行是合法 JSON 且带 `error`，
        调用方会把它当成上游失败 —— 那种情况不能静默吞掉。
        """
        if not line or not line.startswith("data:"):
            return None
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return None
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return None
        return chunk if isinstance(chunk, dict) else None


def _parse_sse_line(line: str) -> tuple[str, dict | None]:
    """返回 ("data", chunk) / ("done", None) / ("other", None)，不抛异常。"""
    if not line or not line.startswith("data:"):
        return "other", None
    data = line[len("data:") :].strip()
    if not data:
        return "other", None
    if data == "[DONE]":
        return "done", None
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return "other", None
    return ("data", chunk) if isinstance(chunk, dict) else ("other", None)


def _provider_request_id(headers: httpx.Headers) -> str | None:
    for name in ("x-request-id", "request-id", "openai-request-id"):
        value = headers.get(name)
        if value:
            return value[:128]
    return None


def _category_from_error(exc: LLMError) -> str:
    message = str(exc)
    if "超时" in message:
        return "timeout"
    if "连接失败" in message:
        return "connection_error"
    if "模型返回 " in message:
        return "http_error"
    if "包含错误" in message:
        return "model_error"
    return "model_error"


def _attempt_snapshot(
    diagnostics: StreamDiagnostics, attempt: int
) -> dict[str, Any]:
    """记录一次尝试开始时的累计值与本次专属事实。

    评估需要回答“第一次为什么空、第二次为什么成功”。只保留最终聚合值会把
    第一次的 ``reasoning_only`` 抹掉，因此每次尝试都留一条可比较的记录。
    """
    return {
        "attempt": attempt,
        "dataEventStart": diagnostics.data_event_count,
        "emptyChoiceStart": diagnostics.empty_choice_count,
        "contentDeltaStart": diagnostics.content_delta_count,
        "reasoningDeltaStart": diagnostics.reasoning_delta_count,
        "contentCharStart": diagnostics.content_chars,
        "httpStatus": None,
        "finishReason": None,
        "outcome": None,
        "emptyReason": None,
    }


def _finish_attempt(
    attempt: dict[str, Any], diagnostics: StreamDiagnostics
) -> None:
    attempt["dataEventCount"] = (
        diagnostics.data_event_count - attempt.pop("dataEventStart")
    )
    attempt["emptyChoiceCount"] = (
        diagnostics.empty_choice_count - attempt.pop("emptyChoiceStart")
    )
    attempt["contentDeltaCount"] = (
        diagnostics.content_delta_count - attempt.pop("contentDeltaStart")
    )
    attempt["reasoningDeltaCount"] = (
        diagnostics.reasoning_delta_count - attempt.pop("reasoningDeltaStart")
    )
    attempt["contentChars"] = (
        diagnostics.content_chars - attempt.pop("contentCharStart")
    )
    attempt["httpStatus"] = diagnostics.http_status
    attempt["finishReason"] = diagnostics.finish_reason


def _empty_reason(
    diagnostics: StreamDiagnostics,
    attempt: dict[str, Any] | None = None,
) -> str:
    """把空流拆成可行动的原因，而不是笼统的“模型没返回”。

    传入单次尝试快照时按本次计数判断；否则回退到累计值，兼容直接调用。
    """
    reasoning_deltas = (
        attempt["reasoningDeltaCount"]
        if attempt is not None
        else diagnostics.reasoning_delta_count
    )
    data_events = (
        attempt["dataEventCount"]
        if attempt is not None
        else diagnostics.data_event_count
    )
    empty_choices = (
        attempt["emptyChoiceCount"]
        if attempt is not None
        else diagnostics.empty_choice_count
    )
    finish_reason = (
        attempt["finishReason"] if attempt is not None else diagnostics.finish_reason
    )
    if reasoning_deltas > 0:
        return "reasoning_only"
    if data_events == 0:
        content_type = (diagnostics.content_type or "").lower()
        if content_type and "event-stream" not in content_type:
            return "format_incompatible"
        return "no_data_events"
    if empty_choices > 0:
        return "empty_choices"
    if finish_reason:
        return "finished_without_content"
    return "content_empty"


def _completion_empty_reason(diagnostics: CompletionDiagnostics) -> str:
    if diagnostics.reasoning_chars > 0 and diagnostics.finish_reason == "length":
        return "reasoning_exhausted_budget"
    if diagnostics.finish_reason == "length":
        return "length_limited"
    if diagnostics.reasoning_chars > 0:
        return "reasoning_only"
    if diagnostics.finish_reason:
        return "finished_without_content"
    return "content_empty"


def _error_text(error: object) -> str:
    """上游 error payload 的安全摘要：不打印请求头，也不假设它一定是字符串。"""
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error
        return str(message)[:500]
    return str(error)[:500]
