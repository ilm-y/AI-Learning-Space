"""OpenAI 兼容适配器的离线契约测试。

全部走 `httpx.MockTransport`：不启动网络、不读取真实 Key，也不会因为本机
`.env` 配了真实模型而意外发起外部调用。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.llm.base import ChatMessage, LLMError
from app.llm.factory import build_llm_adapter
from app.llm.mock import MockLLMAdapter
from app.llm.openai_compatible import OpenAICompatibleAdapter


async def adapter_with_transport(handler) -> OpenAICompatibleAdapter:
    adapter = OpenAICompatibleAdapter(
        base_url="http://llm.test/v1",
        api_key="test-key-not-real",
        model="test-model",
        temperature=0.1,
        max_tokens=64,
        timeout_seconds=5,
    )
    # 构造器创建的 AsyncClient 没有被使用过，先关掉，避免资源警告。
    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


def sse(*payloads: str) -> str:
    lines = []
    for payload in payloads:
        lines.append(f"data: {payload}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


async def test_complete_parses_content_and_sends_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "模型回答"}}]},
        )

    adapter = await adapter_with_transport(handler)
    try:
        answer = await adapter.complete([ChatMessage("user", "你好")])
    finally:
        await adapter.aclose()

    assert answer == "模型回答"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key-not-real"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [{"role": "user", "content": "你好"}]


async def test_stream_parses_deltas_and_ignores_keepalive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = sse(
            json.dumps({"choices": [{"delta": {"content": "你"}}]}),
            "not-json",
            json.dumps({"choices": [{"delta": {"content": "好"}}]}),
            json.dumps({"choices": []}),
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    adapter = await adapter_with_transport(handler)
    try:
        deltas = [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
    finally:
        await adapter.aclose()

    assert deltas == ["你", "好"]


async def test_stream_error_payload_is_not_silently_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse(json.dumps({"error": {"message": "quota exceeded"}})),
        )

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
    finally:
        await adapter.aclose()

    assert exc_info.value.status_code == 502
    assert "quota exceeded" in str(exc_info.value)


async def test_http_error_maps_to_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            await adapter.complete([ChatMessage("user", "hi")])
    finally:
        await adapter.aclose()

    assert exc_info.value.status_code == 502
    assert "429" in str(exc_info.value)


async def test_timeout_maps_to_llm_error_for_complete_and_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            await adapter.complete([ChatMessage("user", "hi")])
        assert exc_info.value.status_code == 502
        assert "超时" in str(exc_info.value)

        with pytest.raises(LLMError) as stream_exc:
            [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
        assert stream_exc.value.status_code == 502
        assert "超时" in str(stream_exc.value)
    finally:
        await adapter.aclose()


async def test_invalid_json_response_maps_to_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            await adapter.complete([ChatMessage("user", "hi")])
    finally:
        await adapter.aclose()

    assert exc_info.value.status_code == 502
    assert "JSON" in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": 123}}]},
    ],
)
async def test_malformed_complete_payload_maps_to_llm_error(payload) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError):
            await adapter.complete([ChatMessage("user", "hi")])
    finally:
        await adapter.aclose()


def test_factory_requires_key_for_openai_instead_of_falling_back_to_mock() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_api_key="",
    )
    with pytest.raises(ValueError, match="AILS_LLM_API_KEY"):
        build_llm_adapter(settings)


async def test_factory_defaults_to_mock_and_builds_openai_with_key() -> None:
    mock_settings = Settings(_env_file=None, llm_provider="mock")
    assert isinstance(build_llm_adapter(mock_settings), MockLLMAdapter)

    openai_settings = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_api_key="test-key-not-real",
        llm_base_url="http://llm.test/v1",
        llm_model="test-model",
    )
    adapter = build_llm_adapter(openai_settings)
    try:
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert adapter.name == "openai-compatible:test-model"
    finally:
        await adapter.aclose()
