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


async def adapter_with_transport(
    handler, *, max_attempts: int = 1, retry_backoff_seconds: float = 0
) -> OpenAICompatibleAdapter:
    adapter = OpenAICompatibleAdapter(
        base_url="http://llm.test/v1",
        api_key="test-key-not-real",
        model="test-model",
        temperature=0.1,
        max_tokens=64,
        timeout_seconds=5,
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
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
            headers={"x-request-id": "complete-req-1"},
            json={
                "choices": [
                    {"message": {"content": "模型回答"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    adapter = await adapter_with_transport(handler)
    try:
        answer = await adapter.complete([ChatMessage("user", "你好")])
        diag = adapter.last_completion_diagnostics
    finally:
        await adapter.aclose()

    assert answer == "模型回答"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key-not-real"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [{"role": "user", "content": "你好"}]
    assert diag is not None
    assert diag.http_status == 200
    assert diag.provider_request_id == "complete-req-1"
    assert diag.finish_reason == "stop"
    assert diag.usage == {"prompt_tokens": 7, "completion_tokens": 3}
    assert diag.content_chars == len("模型回答")
    assert diag.error_category == "none"


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


async def test_stream_diagnostics_capture_finish_reason_usage_and_provider_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = sse(
            json.dumps({"choices": [{"delta": {"content": "TLB"}}]}),
            json.dumps({"choices": [{"delta": {"content": " 是缓存"}}]}),
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                }
            ),
        )
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "provider-req-1",
            },
            text=body,
        )

    adapter = await adapter_with_transport(handler)
    try:
        deltas = [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
        diag = adapter.last_diagnostics
    finally:
        await adapter.aclose()

    assert deltas == ["TLB", " 是缓存"]
    assert diag is not None
    assert diag.http_status == 200
    assert diag.provider_request_id == "provider-req-1"
    assert diag.content_delta_count == 2
    assert diag.data_event_count == 3
    assert diag.finish_reason == "stop"
    assert diag.usage == {"prompt_tokens": 11, "completion_tokens": 4}
    assert diag.saw_done is True
    assert diag.error_category == "none"
    assert diag.content_chars == len("TLB 是缓存")


async def test_stream_completion_exposes_final_finish_reason_for_truncation() -> None:
    """最后一个非空 finish_reason 必须进入每次调用的终态，而不是只留在诊断字段。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = sse(
            json.dumps({"choices": [{"delta": {"content": "部分回答"}}]}),
            json.dumps(
                {"choices": [{"delta": {}, "finish_reason": "length"}]}
            ),
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    adapter = await adapter_with_transport(handler)
    try:
        stream = adapter.stream_chat_with_completion([ChatMessage("user", "hi")])
        deltas = [item async for item in stream]
    finally:
        await adapter.aclose()

    assert deltas == ["部分回答"]
    assert stream.completion is not None
    assert stream.completion.metadata_available is True
    assert stream.completion.finish_reason == "length"
    assert stream.completion.truncated is True


async def test_stream_completion_is_normal_after_stop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = sse(
            json.dumps({"choices": [{"delta": {"content": "完整回答"}}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    adapter = await adapter_with_transport(handler)
    try:
        stream = adapter.stream_chat_with_completion([ChatMessage("user", "hi")])
        [item async for item in stream]
    finally:
        await adapter.aclose()

    assert stream.completion is not None
    assert stream.completion.finish_reason == "stop"
    assert stream.completion.truncated is False


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(json.dumps({"choices": []}), json.dumps({"choices": []})),
            ),
            "empty_choices",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(json.dumps({"choices": [{"delta": {"reasoning_content": "思考中"}}]})),
            ),
            "reasoning_only",
        ),
        (
            # 请求了 stream，但服务端回了普通 JSON：不是 SSE，不能当成“模型没说话”。
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"choices": [{"message": {"content": "完整回答"}}]},
            ),
            "format_incompatible",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="",
            ),
            "no_data_events",
        ),
    ],
)
async def test_empty_stream_is_identifiable(response, reason: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
        diag = adapter.last_diagnostics
    finally:
        await adapter.aclose()

    assert "空流" in str(exc_info.value)
    assert reason in str(exc_info.value)
    assert exc_info.value.status_code == 502
    assert diag is not None
    assert diag.error_category == "empty_stream"
    assert diag.empty_reason == reason
    assert diag.content_delta_count == 0
    # HTTP 200 只说明传输成功，不代表模型完成了生成。
    assert diag.http_status == 200


async def test_empty_stream_retries_once_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(json.dumps({"choices": []})),
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse(json.dumps({"choices": [{"delta": {"content": "第二次成功"}}]})),
        )

    adapter = await adapter_with_transport(handler, max_attempts=2)
    try:
        deltas = [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
        diag = adapter.last_diagnostics
    finally:
        await adapter.aclose()

    assert deltas == ["第二次成功"]
    assert calls["n"] == 2
    assert diag is not None
    assert diag.attempt == 2
    assert diag.error_category == "none"
    # 成功重试后不能残留“可重试”或第一次尝试的空流原因。
    assert diag.retryable is False
    assert diag.empty_reason is None
    assert [item["outcome"] for item in diag.attempt_history] == ["empty", "content"]
    assert diag.attempt_history[0]["emptyReason"] == "empty_choices"
    assert diag.attempt_history[0]["httpStatus"] == 200
    assert diag.attempt_history[1]["contentDeltaCount"] == 1


async def test_empty_complete_is_an_identifiable_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "empty-complete-1"},
            json={
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 0},
            },
        )

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            await adapter.complete([ChatMessage("user", "hi")])
        diag = adapter.last_completion_diagnostics
    finally:
        await adapter.aclose()

    assert exc_info.value.status_code == 502
    assert "空内容" in str(exc_info.value)
    assert "finished_without_content" in str(exc_info.value)
    assert diag is not None
    assert diag.error_category == "empty_completion"
    assert diag.empty_reason == "finished_without_content"
    assert diag.http_status == 200
    assert diag.provider_request_id == "empty-complete-1"
    assert diag.usage == {"prompt_tokens": 9, "completion_tokens": 0}


async def test_complete_reasoning_exhausting_token_budget_is_classified() -> None:
    """真实模型可能把 max_tokens 全花在 reasoning_content 上。

    HTTP 200 + finish_reason=length 不是“成功”。这里只验证分类，不改
    max_tokens，也不把 reasoning 文本当成用户可见回答。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "先分析一下这个问题的背景……",
                        },
                        "finish_reason": "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 64,
                    "completion_tokens_details": {"reasoning_tokens": 56},
                },
            },
        )

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            await adapter.complete([ChatMessage("user", "请分析")])
        diag = adapter.last_completion_diagnostics
    finally:
        await adapter.aclose()

    assert "reasoning_exhausted_budget" in str(exc_info.value)
    assert exc_info.value.status_code == 502
    assert diag is not None
    assert diag.error_category == "empty_completion"
    assert diag.empty_reason == "reasoning_exhausted_budget"
    assert diag.finish_reason == "length"
    assert diag.content_chars == 0
    assert diag.reasoning_chars > 0
    assert diag.usage["completion_tokens_details"]["reasoning_tokens"] == 56


@pytest.mark.parametrize("content", [None, "", "   \n"])
async def test_complete_null_or_blank_content_never_returns_empty_string(content) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    adapter = await adapter_with_transport(handler)
    try:
        with pytest.raises(LLMError) as exc_info:
            await adapter.complete([ChatMessage("user", "hi")])
    finally:
        await adapter.aclose()

    assert exc_info.value.status_code == 502
    assert "空内容" in str(exc_info.value)


async def test_empty_stream_retry_is_bounded() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse(json.dumps({"choices": []})),
        )

    adapter = await adapter_with_transport(handler, max_attempts=2)
    try:
        with pytest.raises(LLMError) as exc_info:
            [item async for item in adapter.stream_chat([ChatMessage("user", "hi")])]
        diag = adapter.last_diagnostics
    finally:
        await adapter.aclose()

    assert calls["n"] == 2
    assert "attempts=2" in str(exc_info.value)
    assert diag is not None
    assert diag.attempt == 2


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
    assert adapter.last_diagnostics is not None
    assert adapter.last_diagnostics.error_category == "model_error"
    assert adapter.last_diagnostics.http_status == 200


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
