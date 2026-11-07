"""知识检索离线测试：不连接真实 Qdrant / 后端 / Embedding API。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.embedding import HashEmbeddingProvider
from app.retriever.base import (
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
    RetrievalError,
)
from app.retriever.fixture import FixtureRetriever
from app.retriever.http_backend import HttpBackendRetriever
from app.retriever.knowledge import AllowedDocument, KnowledgeSearchService
from app.retriever.routing import FallbackRetriever
from app.vectorstore.qdrant_store import KnowledgeSearchHit

MODEL = "hash-v1"


class FakeStore:
    def __init__(self, hits: list[KnowledgeSearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def search_knowledge(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.hits


class FailingProvider(HashEmbeddingProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__(dimension=8)
        self.error = error

    async def embed(self, texts):
        raise self.error


class SlowProvider(HashEmbeddingProvider):
    async def embed(self, texts):
        await asyncio.sleep(0.2)
        return [[0.0] * self.dimension for _ in texts]


def hit(
    *,
    chunk_id: int = 11,
    document_id: int = 101,
    chunk_index: int = 0,
    score: float = 0.9,
    document_name: str = "操作系统笔记.md",
    content: str = "TLB 是页表项缓存。",
    index_version: int | None = None,
    doc_version: str | None = None,
    embedding_fingerprint: str | None = None,
) -> KnowledgeSearchHit:
    payload = {
        "user_id": 7,
        "space_id": 9,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "file_name": document_name,
        "file_type": "md",
        "content": content,
        "metadata": {"headingPath": ["虚拟内存", "TLB"]},
    }
    if index_version is not None:
        payload["index_version"] = index_version
    if doc_version is not None:
        payload["doc_version"] = doc_version
    if embedding_fingerprint is not None:
        payload["embedding_fingerprint"] = embedding_fingerprint
    return KnowledgeSearchHit(score=score, payload=payload)


def service(
    *,
    provider=None,
    store: FakeStore | None = None,
    timeout: float = 5.0,
) -> KnowledgeSearchService:
    return KnowledgeSearchService(
        provider=provider or HashEmbeddingProvider(dimension=8),
        store=store or FakeStore(),
        collection_prefix="test_docs",
        timeout_seconds=timeout,
    )


def query(*, top_k: int = 5, min_score: float = 0.35) -> KnowledgeQuery:
    return KnowledgeQuery(
        user_id=7,
        space_id=9,
        question="TLB 是什么？",
        top_k=top_k,
        min_score=min_score,
    )


async def test_success_returns_traceable_fields_and_filters_by_threshold() -> None:
    store = FakeStore(
        hits=[
            hit(chunk_id=11, chunk_index=2, score=0.91),
            hit(chunk_id=12, chunk_index=3, score=0.10, content="无关内容"),
        ]
    )
    result = await service(store=store).search(query(min_score=0.35), [AllowedDocument(101, 0)])

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [11]
    chunk = result.chunks[0]
    assert chunk.document_id == 101
    assert chunk.chunk_index == 2
    assert chunk.document_name == "操作系统笔记.md"
    assert chunk.file_type == "md"
    assert chunk.heading_path == ["虚拟内存", "TLB"]
    assert chunk.score == pytest.approx(0.91)
    assert result.candidates == 2
    assert store.calls[0]["user_id"] == 7
    assert store.calls[0]["space_id"] == 9
    assert store.calls[0]["document_versions"] == [(101, 0)]
    assert store.calls[0]["top_k"] == 5


async def test_all_hits_below_threshold_are_reported_not_faked_as_success() -> None:
    store = FakeStore(hits=[hit(score=0.2)])
    result = await service(store=store).search(query(min_score=0.35), [AllowedDocument(101, 0)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.THRESHOLD_FILTERED
    assert "低于阈值" in (result.warning or "")
    assert result.candidates == 1


async def test_empty_allowlist_never_calls_embedding_or_qdrant() -> None:
    store = FakeStore()
    provider = FailingProvider(RuntimeError("must not embed"))
    result = await service(provider=provider, store=store).search(query(), [])

    assert result.status is KnowledgeStatus.NO_VALID_DOCUMENTS
    assert store.calls == []


async def test_malformed_allowlist_fails_closed() -> None:
    store = FakeStore()
    result = await service(store=store).search(query(), [AllowedDocument(101, 0), 0])  # type: ignore[list-item]

    assert result.status is KnowledgeStatus.DEGRADED
    assert store.calls == []


async def test_payload_outside_allowlist_is_rejected() -> None:
    store = FakeStore(hits=[hit(document_id=999, score=0.9)])
    result = await service(store=store).search(query(), [AllowedDocument(101, 0)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED
    assert "字段不完整" in (result.warning or "")


async def test_payload_from_inactive_version_is_rejected() -> None:
    # 白名单活跃版本是 3，但 Qdrant 返回了旧版本 2 的 point。
    store = FakeStore(hits=[hit(index_version=2, doc_version="101:2", score=0.9)])
    result = await service(store=store).search(query(), [AllowedDocument(101, 3)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED


async def test_payload_with_matching_active_version_is_accepted() -> None:
    store = FakeStore(hits=[hit(index_version=3, doc_version="101:3", score=0.9)])
    result = await service(store=store).search(query(), [AllowedDocument(101, 3)])

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.document_id for chunk in result.chunks] == [101]


async def test_payload_with_incompatible_embedding_fingerprint_is_rejected() -> None:
    store = FakeStore(
        hits=[hit(embedding_fingerprint="v1:another-gateway", score=0.9)]
    )
    result = await service(store=store).search(query(), [AllowedDocument(101, 0)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED


async def test_missing_payload_fields_degrade_to_empty_knowledge() -> None:
    broken = KnowledgeSearchHit(score=0.9, payload={"document_id": 101})
    result = await service(store=FakeStore(hits=[broken])).search(query(), [AllowedDocument(101, 0)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED


async def test_embedding_failure_degrades() -> None:
    result = await service(
        provider=FailingProvider(RuntimeError("embedding down"))
    ).search(query(), [AllowedDocument(101, 0)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED


async def test_qdrant_failure_degrades() -> None:
    store = FakeStore()
    store.error = RuntimeError("qdrant down")
    result = await service(store=store).search(query(), [AllowedDocument(101, 0)])

    assert result.status is KnowledgeStatus.DEGRADED
    assert "依赖不可用" in (result.warning or "")


async def test_retrieval_timeout_degrades() -> None:
    result = await service(
        provider=SlowProvider(dimension=8), timeout=0.01
    ).search(query(), [AllowedDocument(101, 0)])

    assert result.status is KnowledgeStatus.DEGRADED
    assert "超时" in (result.warning or "")


# ===================== HTTP 白名单 =====================


class RecordingSearch(KnowledgeSearchService):
    def __init__(self, result: KnowledgeResult | None = None) -> None:
        super().__init__(
            provider=HashEmbeddingProvider(dimension=8),
            store=FakeStore(),
            collection_prefix="test_docs",
            timeout_seconds=5,
        )
        self.result = result or KnowledgeResult(status=KnowledgeStatus.NO_MATCH)
        self.calls: list[tuple[KnowledgeQuery, list[AllowedDocument]]] = []

    @property
    def embedding_model(self) -> str:
        return MODEL

    async def search(self, query, allowed_documents):
        if not allowed_documents:
            # 与真实实现一致：空白名单在进入 Qdrant 之前就返回。
            return KnowledgeResult(status=KnowledgeStatus.NO_VALID_DOCUMENTS)
        self.calls.append((query, allowed_documents))
        return self.result


def http_retriever(handler, search: RecordingSearch) -> HttpBackendRetriever:
    retriever = HttpBackendRetriever(
        "http://backend.test",
        auth_token="user-jwt",
        knowledge_search=search,
    )
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever


async def test_http_ready_ids_are_used_and_model_is_sent() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["model"] = request.url.params.get("embeddingModel")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"code": 0, "data": [101, 202]})

    search = RecordingSearch(KnowledgeResult(chunks=[]))
    retriever = http_retriever(handler, search)
    try:
        await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert seen["path"] == "/api/spaces/9/documents/ready-ids"
    assert seen["model"] == MODEL
    assert seen["auth"] == "Bearer user-jwt"
    assert search.calls[0][1] == [AllowedDocument(101, 0), AllowedDocument(202, 0)]


async def test_http_ready_ids_accepts_active_index_versions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {"id": 101, "indexVersion": 3},
                    {"id": 202, "indexVersion": 1},
                ],
            },
        )

    search = RecordingSearch(KnowledgeResult(chunks=[]))
    retriever = http_retriever(handler, search)
    try:
        await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert search.calls[0][1] == [AllowedDocument(101, 3), AllowedDocument(202, 1)]


@pytest.mark.parametrize(
    "data",
    [
        [{"id": 101, "indexVersion": 3}, {"id": 101, "indexVersion": 4}],
        [{"id": 0, "indexVersion": 1}],
        [{"id": 101, "indexVersion": -1}],
        [{"id": "101", "indexVersion": 1}],
    ],
)
async def test_http_ready_ids_rejects_malformed_versions(data) -> None:
    search = RecordingSearch()
    retriever = http_retriever(
        lambda request: httpx.Response(200, json={"code": 0, "data": data}), search
    )
    try:
        result = await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert result.status is KnowledgeStatus.DEGRADED
    assert search.calls == []


async def test_fatal_401_from_backend_is_retrieval_error() -> None:
    """委托声明过期时后端返回 401，必须映射成可被 Worker 归类的 RetrievalError。"""
    retriever = http_retriever(
        lambda request: httpx.Response(401, json={"code": 401, "message": "unauthorized"}),
        RecordingSearch(),
    )
    try:
        with pytest.raises(RetrievalError, match="401"):
            await retriever.list_concepts(9)
    finally:
        await retriever.aclose()


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (401, {"code": 401, "message": "unauthorized"}),
        (500, {"code": 500, "message": "down"}),
        (200, {"code": 0, "data": "not-a-list"}),
        (200, {"code": 0, "data": [101, "bad"]}),
    ],
)
async def test_allowlist_failure_fails_closed_without_search(status_code, payload) -> None:
    search = RecordingSearch()
    retriever = http_retriever(lambda request: httpx.Response(status_code, json=payload), search)
    try:
        result = await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert result.status is KnowledgeStatus.DEGRADED
    assert search.calls == []


async def test_empty_ready_ids_returns_no_valid_documents_without_search() -> None:
    search = RecordingSearch()
    retriever = http_retriever(
        lambda request: httpx.Response(200, json={"code": 0, "data": []}), search
    )
    try:
        result = await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert result.status is KnowledgeStatus.NO_VALID_DOCUMENTS
    assert search.calls == []


# ===================== Fallback 不得注入 Fixture 知识 =====================


class RaisingRetriever:
    name = "raising"

    async def get_knowledge(self, query):
        raise RetrievalError("backend down")

    async def aclose(self):
        return None


class FixtureKnowledge:
    name = "fixture"

    async def get_knowledge(self, query):
        return KnowledgeResult(
            chunks=[
                # 这里故意放一条内容；若 Fallback 调用它，测试会失败。
                type("Chunk", (), {"id": 1})()
            ],
            status=KnowledgeStatus.SUCCESS,
        )

    async def aclose(self):
        return None


async def test_fallback_never_uses_fixture_for_knowledge() -> None:
    retriever = FallbackRetriever(RaisingRetriever(), FixtureKnowledge())
    try:
        result = await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED
    assert retriever.degraded is False


async def test_fixture_mode_still_supports_knowledge() -> None:
    from pathlib import Path

    fixture = FixtureRetriever(Path(__file__).resolve().parents[1] / "fixtures/demo_space.json")
    result = await fixture.get_knowledge(
        KnowledgeQuery(
            user_id=1,
            space_id=1,
            question="TLB 是什么？",
            concept_ids=(10,),
        )
    )
    await fixture.aclose()

    assert result.status is KnowledgeStatus.SUCCESS
    assert result.chunks
    assert result.chunks[0].document_name == "操作系统笔记.md"
