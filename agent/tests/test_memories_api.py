"""内部记忆向量接口测试（FastAPI TestClient，不依赖真实 Qdrant）。

FakeStore 记录调用参数，Embedding 用离线 Hash。重点验证三件事：

1. 写入 payload 的字段契约，且**绝不包含正文**；
2. `userId` 不能越过签名身份，缺失凭据 fail closed；
3. DELETE 幂等、UPSERT 内容未变时跳过重嵌入。
"""

from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.documents.errors import DocumentEmbeddingError
from app.embedding import HashEmbeddingProvider
from app.internal_identity import mint_identity
from app.memories.errors import MemoryVectorStoreUnavailable
from app.memories.indexer import MemoryIndexer
from app.memories.routes import router
from app.memories.search import MemoryVectorSearch
from app.vectorstore.memory_store import MemoryVectorHit, point_id_for_memory

TOKEN = "internal-test-token"
IDENTITY_SECRET = "internal-test-identity-secret"
HEADERS = {
    "X-Internal-Token": TOKEN,
    "X-Internal-User-Id": "1",
    "X-Internal-Identity": mint_identity(
        1, secret=IDENTITY_SECRET, ttl_seconds=300
    ),
}
DIMENSION = 8


class FakeStore:
    def __init__(
        self,
        *,
        fail: bool = False,
        hits: list[MemoryVectorHit] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.fail = fail
        self.hits = hits or []
        self.warnings = warnings or []
        self.ensured: tuple[str, int] | None = None
        self.upserts: list[dict] = []
        self.deleted: list[dict] = []
        self.payloads: dict[str, dict] = {}
        self.search_calls: list[dict] = []

    def collection_name(self, *, model: str, dimension: int) -> str:
        return f"test_{model}_{dimension}"

    async def ensure_collection(self, *, model: str, dimension: int) -> str:
        self.ensured = (model, dimension)
        return self.collection_name(model=model, dimension=dimension)

    async def retrieve_payload(
        self, *, collection_name: str, point_id: str
    ) -> dict | None:
        return self.payloads.get(point_id)

    async def upsert_memory(self, **kwargs) -> None:
        if self.fail:
            raise MemoryVectorStoreUnavailable("qdrant unavailable")
        self.upserts.append(kwargs)

    async def delete_memory(self, *, collection_name: str, memory_id: int) -> None:
        if self.fail:
            raise MemoryVectorStoreUnavailable("qdrant unavailable")
        self.deleted.append(
            {"collection": collection_name, "memoryId": memory_id}
        )

    async def search(self, **kwargs):
        if self.fail:
            raise MemoryVectorStoreUnavailable("qdrant unavailable")
        self.search_calls.append(kwargs)
        return self.hits, self.warnings


class FailingEmbedding(HashEmbeddingProvider):
    async def embed(self, texts):
        raise DocumentEmbeddingError("embedding unavailable")


def make_client(
    *,
    store: FakeStore | None = None,
    provider: HashEmbeddingProvider | None = None,
    settings: Settings | None = None,
):
    store = store or FakeStore()
    provider = provider or HashEmbeddingProvider(dimension=DIMENSION)
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings or Settings(
        _env_file=None,
        internal_token=TOKEN,
        internal_identity_secret=IDENTITY_SECRET,
        embedding_provider="hash",
        embedding_dimension=DIMENSION,
    )
    app.state.embedding_provider = provider
    app.state.memory_store = store
    app.state.memory_indexer = MemoryIndexer(provider=provider, store=store)
    app.state.memory_search = MemoryVectorSearch(
        provider=provider,
        store=store,
        timeout_seconds=5.0,
    )
    return TestClient(app)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_sync_upsert_writes_payload_without_content() -> None:
    store = FakeStore()
    client = make_client(store=store)

    response = client.post(
        "/agent/memories/sync",
        json={
            "memoryId": 42,
            "userId": 1,
            "action": "UPSERT",
            "content": "  用户偏好用递归解释分治  ",
            "status": "ACTIVE",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["skipped"] is False
    assert body["data"]["pointId"] == point_id_for_memory(42)
    assert store.ensured == ("hash-v1", DIMENSION)

    written = store.upserts[0]
    payload = written["payload"]
    assert written["point_id"] == point_id_for_memory(42)
    assert len(written["vector"]) == DIMENSION
    assert payload["memory_id"] == 42
    assert payload["user_id"] == 1
    assert payload["status"] == "ACTIVE"
    assert payload["scope_type"] == "GLOBAL"
    assert payload["content_hash"] == _sha256("用户偏好用递归解释分治")
    assert payload["embedding_fingerprint"].startswith("v1:")
    # 正文不进入向量库；检索只需要 id 与分数。
    assert "content" not in payload


def test_sync_upsert_accepts_operation_and_scope_fields() -> None:
    store = FakeStore()
    client = make_client(store=store)

    response = client.post(
        "/agent/memories/sync",
        json={
            "eventId": 9001,
            "operation": "UPSERT",
            "memoryId": 7,
            "userId": 1,
            "content": "空间内的偏好",
            "contentHash": "abc123",
            "memoryVersion": 3,
            "scopeType": "CONCEPT",
            "learningSpaceId": 9,
            "conceptId": 5,
            "memoryType": "LEARNING",
            "importance": 0.8,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    payload = store.upserts[0]["payload"]
    assert payload["scope_type"] == "CONCEPT"
    assert payload["space_id"] == 9
    assert payload["concept_id"] == 5
    assert payload["memory_type"] == "LEARNING"
    assert payload["importance"] == 0.8
    assert payload["memory_version"] == 3
    assert payload["content_hash"] == "abc123"


def test_sync_skips_unchanged_payload_without_reembedding() -> None:
    store = FakeStore()
    provider = HashEmbeddingProvider(dimension=DIMENSION)
    desired = {
        "memory_id": 42,
        "user_id": 1,
        "status": "ACTIVE",
        "scope_type": "GLOBAL",
        "space_id": None,
        "concept_id": None,
        "memory_type": None,
        "importance": None,
        "content_hash": "same-hash",
        "memory_version": None,
        "embedding_fingerprint": provider.fingerprint,
    }
    store.payloads[point_id_for_memory(42)] = desired
    client = make_client(store=store, provider=provider)

    response = client.post(
        "/agent/memories/sync",
        json={
            "memoryId": 42,
            "userId": 1,
            "operation": "UPSERT",
            "content": "内容没变",
            "contentHash": "same-hash",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["data"]["skipped"] is True
    assert store.upserts == []


def test_sync_delete_does_not_create_collection_or_embed() -> None:
    store = FakeStore()
    client = make_client(store=store)

    response = client.post(
        "/agent/memories/sync",
        json={"memoryId": 42, "userId": 1, "action": "DELETE"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["pointId"] is None
    assert data["collection"] == "test_hash-v1_8"
    assert store.deleted == [
        {"collection": "test_hash-v1_8", "memoryId": 42}
    ]
    # DELETE 不应该为了清理旧数据而创建空 collection。
    assert store.ensured is None


def test_sync_rejects_user_mismatch() -> None:
    store = FakeStore()
    client = make_client(store=store)

    response = client.post(
        "/agent/memories/sync",
        json={"memoryId": 42, "userId": 2, "operation": "DELETE"},
        headers=HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["code"] == 403
    assert store.deleted == []


def test_sync_requires_internal_token() -> None:
    client = make_client()
    response = client.post(
        "/agent/memories/sync",
        json={"memoryId": 42, "userId": 1, "operation": "DELETE"},
    )
    assert response.status_code == 401


def test_sync_fails_closed_when_token_not_configured() -> None:
    settings = Settings(
        _env_file=None,
        internal_token="",
        internal_identity_secret=IDENTITY_SECRET,
        embedding_dimension=DIMENSION,
    )
    client = make_client(settings=settings)
    response = client.post(
        "/agent/memories/sync",
        json={"memoryId": 42, "userId": 1, "operation": "DELETE"},
        headers=HEADERS,
    )
    assert response.status_code == 503


def test_sync_embedding_failure_is_502() -> None:
    client = make_client(provider=FailingEmbedding(dimension=DIMENSION))
    response = client.post(
        "/agent/memories/sync",
        json={
            "memoryId": 42,
            "userId": 1,
            "operation": "UPSERT",
            "content": "x",
        },
        headers=HEADERS,
    )
    assert response.status_code == 502
    assert response.json()["code"] == 502


def test_sync_store_failure_is_503() -> None:
    client = make_client(store=FakeStore(fail=True))
    response = client.post(
        "/agent/memories/sync",
        json={
            "memoryId": 42,
            "userId": 1,
            "operation": "UPSERT",
            "content": "x",
        },
        headers=HEADERS,
    )
    assert response.status_code == 503


def test_sync_rejects_unknown_operation() -> None:
    client = make_client()
    response = client.post(
        "/agent/memories/sync",
        json={
            "memoryId": 42,
            "userId": 1,
            "operation": "MERGE",
            "content": "x",
        },
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_sync_upsert_requires_content() -> None:
    client = make_client()
    response = client.post(
        "/agent/memories/sync",
        json={"memoryId": 42, "userId": 1, "operation": "UPSERT"},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_search_returns_only_ids_scores_and_hashes() -> None:
    provider = HashEmbeddingProvider(dimension=DIMENSION)
    store = FakeStore(
        hits=[
            MemoryVectorHit(42, 0.87, "hash-42", provider.fingerprint),
            MemoryVectorHit(43, 0.81, "hash-43", "v1:different-vector-space"),
            MemoryVectorHit(44, 0.12, "hash-44", provider.fingerprint),
        ]
    )
    client = make_client(store=store, provider=provider)

    response = client.post(
        "/agent/memories/search",
        json={"userId": 1, "query": "递归"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["collection"] == "test_hash-v1_8"
    assert data["hits"] == [
        {"memoryId": 42, "score": 0.87, "contentHash": "hash-42"},
        {"memoryId": 44, "score": 0.12, "contentHash": "hash-44"},
    ]
    assert all("content" not in hit for hit in data["hits"])
    # 指纹不同的候选被丢弃，并给出 warning；44 不是被指纹过滤掉的。
    assert data["warnings"] == ["已忽略 embedding 指纹不匹配的记忆向量"]
    assert store.search_calls[0]["user_id"] == 1
    assert store.search_calls[0]["space_id"] is None
    assert store.search_calls[0]["top_k"] == 20


def test_search_passes_visibility_and_recall() -> None:
    store = FakeStore()
    client = make_client(store=store)

    response = client.post(
        "/agent/memories/search",
        json={
            "requestId": "req-1",
            "userId": 1,
            "spaceId": 9,
            "conceptId": 5,
            "query": "递归",
            "recallK": 5,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    call = store.search_calls[0]
    assert call["space_id"] == 9
    assert call["concept_id"] == 5
    assert call["top_k"] == 5


def test_search_blank_query_is_422() -> None:
    client = make_client()
    response = client.post(
        "/agent/memories/search",
        json={"userId": 1, "query": "   "},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_search_rejects_user_mismatch() -> None:
    client = make_client()
    response = client.post(
        "/agent/memories/search",
        json={"userId": 2, "query": "递归"},
        headers=HEADERS,
    )
    assert response.status_code == 403


def test_search_reports_missing_collection_as_warning() -> None:
    store = FakeStore(warnings=["memory collection 未建立"])
    client = make_client(store=store)
    response = client.post(
        "/agent/memories/search",
        json={"userId": 1, "query": "递归"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["hits"] == []
    assert data["warnings"] == ["memory collection 未建立"]


def test_search_store_failure_is_503() -> None:
    client = make_client(store=FakeStore(fail=True))
    response = client.post(
        "/agent/memories/search",
        json={"userId": 1, "query": "递归"},
        headers=HEADERS,
    )
    assert response.status_code == 503
