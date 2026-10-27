"""OpenAI 兼容适配器。

DeepSeek / OpenAI / 通义千问 / vLLM / Ollama 都提供 `/chat/completions`，
差别只在 base_url、api_key、model 三个配置项，所以这里只写一个实现。

流式解析的是标准 SSE：逐行读 `data: {...}`，遇到 `data: [DONE]` 结束。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.llm.base import ChatMessage, LLMError


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        # 仅用于观测：流式响应本身不携带 HTTP 状态，评估需要知道这次是不是 2xx。
        self.last_status_code: int | None = None

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
        payload = self._payload(messages, temperature, max_tokens, stream=True)
        try:
            async with self._client.stream(
                "POST", self._endpoint, json=payload, headers=self._headers
            ) as response:
                self.last_status_code = response.status_code
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise LLMError(
                        f"模型返回 {response.status_code}：{body[:500]}", status_code=502
                    )
                async for line in response.aiter_lines():
                    chunk = self._parse_chunk(line)
                    if chunk is None:
                        continue
                    if chunk.get("error"):
                        raise LLMError(
                            f"模型流式响应包含错误：{_error_text(chunk.get('error'))}",
                            status_code=502,
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            # 不能让 httpx 的超时冒成未分类异常：Runtime 会把它当成 500「内部错误」，
            # 用户看到的错误码就不再指向「模型上游超时」。
            raise LLMError("模型请求超时", status_code=502) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"模型连接失败：{type(exc).__name__}", status_code=502
            ) from exc

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        try:
            response = await self._client.post(
                self._endpoint, json=payload, headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise LLMError("模型请求超时", status_code=502) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"模型连接失败：{type(exc).__name__}", status_code=502
            ) from exc

        self.last_status_code = response.status_code
        if response.status_code >= 400:
            raise LLMError(
                f"模型返回 {response.status_code}：{response.text[:500]}", status_code=502
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"模型响应不是合法 JSON：{response.text[:500]}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型响应结构不符合预期：{str(body)[:500]}") from exc
        if content is None:
            return ""
        if not isinstance(content, str):
            raise LLMError(f"模型响应 content 不是字符串：{type(content).__name__}")
        return content

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


def _error_text(error: object) -> str:
    """上游 error payload 的安全摘要：不打印请求头，也不假设它一定是字符串。"""
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error
        return str(message)[:500]
    return str(error)[:500]
