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
        async with self._client.stream("POST", self._endpoint, json=payload, headers=self._headers) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise LLMError(f"模型返回 {response.status_code}：{body[:500]}", status_code=502)
            async for line in response.aiter_lines():
                delta = self._parse_line(line)
                if delta is not None:
                    yield delta

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        response = await self._client.post(self._endpoint, json=payload, headers=self._headers)
        if response.status_code >= 400:
            raise LLMError(f"模型返回 {response.status_code}：{response.text[:500]}", status_code=502)
        body = response.json()
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型响应结构不符合预期：{str(body)[:500]}") from exc

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
    def _parse_line(line: str) -> str | None:
        """把一行 SSE 变成增量文本；不是内容行就返回 None。"""
        if not line or not line.startswith("data:"):
            return None
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return None
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            # 兼容少数服务推送的 keep-alive 空行，不因为一行解析失败就中断整段回答
            return None
        choices = chunk.get("choices") or []
        if not choices:
            return None
        return choices[0].get("delta", {}).get("content") or None
