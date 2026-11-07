"""Embedding Provider 离线测试：不调用任何真实 API。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.documents.errors import DocumentEmbeddingError
from app.embedding import (
    HashEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
    validate_vectors,
)
from app.embedding.base import embedding_fingerprint


async def test_hash_provider_is_deterministic_and_dimensioned() -> None:
    provider = HashEmbeddingProvider(dimension=32)

    first = await provider.embed(["hello", "世界"])
    second = await provider.embed(["hello", "世界"])

    assert provider.name == "hash"
    assert provider.model == "hash-v1"
    assert len(first) == 2
    assert all(len(vector) == 32 for vector in first)
    assert first == second
    assert first[0] != first[1]


def test_factory_hash_model_name_cannot_be_overridden() -> None:
    settings = Settings(
        _env_file=None,
        embedding_provider="hash",
        embedding_model="text-embedding-3-small",
        embedding_dimension=16,
    )
    provider = build_embedding_provider(settings)

    assert isinstance(provider, HashEmbeddingProvider)
    assert provider.model == "hash-v1"
    assert provider.dimension == 16


def test_factory_rejects_unknown_provider() -> None:
    settings = Settings(_env_file=None, embedding_provider="mystery")
    with pytest.raises(ValueError, match="未知的 Embedding provider"):
        build_embedding_provider(settings)


def test_embedding_fingerprint_changes_with_incompatible_configuration() -> None:
    base = HashEmbeddingProvider(dimension=32).fingerprint

    assert base.startswith("v1:")
    assert base == HashEmbeddingProvider(dimension=32).fingerprint
    # 维度、模型或 provider 变化都会改变向量空间，指纹必须不同。
    assert base != HashEmbeddingProvider(dimension=64).fingerprint
    assert base != embedding_fingerprint(
        provider="hash", model="hash-v2", dimension=32
    )
    assert base != embedding_fingerprint(
        provider="openai-compatible", model="hash-v1", dimension=32
    )


def test_openai_fingerprint_ignores_credentials_but_not_gateway() -> None:
    def provider(base_url: str, api_key: str = "secret") -> OpenAICompatibleEmbeddingProvider:
        return OpenAICompatibleEmbeddingProvider(
            base_url=base_url,
            api_key=api_key,
            model="text-embedding-v4",
            dimension=1024,
            timeout_seconds=5,
            max_retries=0,
        )

    same = provider("https://dashscope.example.com/v1/", api_key="a").fingerprint
    assert same == provider("https://DASHSCOPE.example.com/v1", api_key="b").fingerprint
    # 不同网关即使模型名与维度相同，也不能共用向量空间。
    assert same != provider("https://other.example.com/v1").fingerprint


def test_validate_vectors_rejects_count_dimension_and_non_finite() -> None:
    with pytest.raises(DocumentEmbeddingError, match="数量不匹配"):
        validate_vectors([[0.1, 0.2]], expected_count=2, dimension=2, provider="test")
    with pytest.raises(DocumentEmbeddingError, match="维度不匹配"):
        validate_vectors([[0.1]], expected_count=1, dimension=2, provider="test")
    with pytest.raises(DocumentEmbeddingError, match="NaN/Infinity"):
        validate_vectors([[0.1, float("nan")]], expected_count=1, dimension=2, provider="test")
    with pytest.raises(DocumentEmbeddingError, match="NaN/Infinity"):
        validate_vectors(
            [[0.1, float("inf")]], expected_count=1, dimension=2, provider="test"
        )
    with pytest.raises(DocumentEmbeddingError, match="非数值"):
        validate_vectors([["x", 0.2]], expected_count=1, dimension=2, provider="test")


def _provider(
    handler,
    *,
    max_retries: int = 0,
    batch_size: int = 10,
) -> OpenAICompatibleEmbeddingProvider:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://embedding.test/v1",
        api_key="test-key",
        model="embed-model",
        dimension=3,
        timeout_seconds=5,
        max_retries=max_retries,
        retry_backoff_seconds=0,
        batch_size=batch_size,
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


async def test_openai_provider_orders_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "embed-model"
        assert body["input"] == ["a", "b"]
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                ]
            },
        )

    provider = _provider(handler)
    try:
        vectors = await provider.embed(["a", "b"])
    finally:
        await provider.aclose()

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


async def test_openai_provider_batches_and_preserves_order() -> None:
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batches.append(body["input"])
        scores = {"a": 0.1, "b": 0.2, "c": 0.3}
        return httpx.Response(
            200,
            json={
                "model": "embed-model",
                "data": [
                    {"index": index, "embedding": [scores[value], 0, 0]}
                    for index, value in enumerate(body["input"])
                ],
            },
        )

    provider = _provider(handler, batch_size=2)
    try:
        vectors = await provider.embed(["a", "b", "c"])
    finally:
        await provider.aclose()

    assert batches == [["a", "b"], ["c"]]
    assert vectors == [[0.1, 0.0, 0.0], [0.2, 0.0, 0.0], [0.3, 0.0, 0.0]]


async def test_openai_provider_rejects_response_model_mismatch() -> None:
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json={
                "model": "another-model",
                "data": [{"index": 0, "embedding": [1, 0, 0]}],
            },
        )
    )
    try:
        with pytest.raises(DocumentEmbeddingError, match="响应不符合"):
            await provider.embed(["a"])
    finally:
        await provider.aclose()


async def test_openai_provider_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0, 0]}]})

    provider = _provider(handler, max_retries=1)
    try:
        vectors = await provider.embed(["a"])
    finally:
        await provider.aclose()

    assert calls == 2
    assert vectors == [[1.0, 0.0, 0.0]]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [1, 0]}]},
        {"data": [{"index": 5, "embedding": [1, 0, 0]}]},
        {"unexpected": True},
    ],
)
async def test_openai_provider_rejects_bad_response(payload) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(DocumentEmbeddingError):
            await provider.embed(["a"])
    finally:
        await provider.aclose()


async def test_openai_provider_rejects_nan_in_raw_json() -> None:
    # Python 的 json 模块默认接受 NaN；这里绕过 json= 序列化，直接构造原始响应。
    provider = _provider(
        lambda request: httpx.Response(
            200, content=b'{"data":[{"index":0,"embedding":[1,NaN,0]}]}'
        )
    )
    try:
        with pytest.raises(DocumentEmbeddingError, match="NaN/Infinity"):
            await provider.embed(["a"])
    finally:
        await provider.aclose()


async def test_openai_provider_does_not_fallback_on_http_error() -> None:
    provider = _provider(lambda request: httpx.Response(400, text="bad request"))
    try:
        with pytest.raises(DocumentEmbeddingError, match="HTTP 400"):
            await provider.embed(["a"])
    finally:
        await provider.aclose()
