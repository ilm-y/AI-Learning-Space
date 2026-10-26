"""内部文档 HTTP 接口测试（FastAPI TestClient，不依赖真实 Qdrant）。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.documents.errors import DocumentEmbeddingError, DocumentVectorStoreError
from app.documents.routes import router
from app.embedding import HashEmbeddingProvider
from app.vectorstore.qdrant_store import point_id_for

TOKEN = "internal-test-token"
HEADERS = {"X-Internal-Token": TOKEN, "X-Internal-User-Id": "1"}


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ensured: tuple[str, int] | None = None
        self.indexed: dict | None = None

    async def ensure_collection(self, *, model: str, dimension: int) -> str:
        self.ensured = (model, dimension)
        return f"test_{model}_{dimension}"

    async def index_document(self, **kwargs) -> dict[int, str]:
        if self.fail:
            raise DocumentVectorStoreError("qdrant unavailable")
        self.indexed = kwargs
        document_id = kwargs["document_id"]
        return {
            chunk.chunk_id: point_id_for(document_id, chunk.chunk_index)
            for chunk in kwargs["chunks"]
        }


class FailingEmbedding(HashEmbeddingProvider):
    async def embed(self, texts):
        raise DocumentEmbeddingError("embedding unavailable")


def make_client(*, store: FakeStore | None = None, settings: Settings | None = None):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings or Settings(
        _env_file=None,
        internal_token=TOKEN,
        embedding_provider="hash",
        embedding_dimension=8,
        document_max_bytes=1024,
    )
    app.state.embedding_provider = HashEmbeddingProvider(dimension=8)
    app.state.qdrant_store = store or FakeStore()
    return TestClient(app)


def test_parse_requires_internal_token() -> None:
    client = make_client()
    response = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "1", "userId": "1"},
        files={"file": ("notes.md", b"# T\n\nhello", "text/markdown")},
    )
    assert response.status_code == 401


def test_parse_fails_closed_when_token_not_configured() -> None:
    settings = Settings(_env_file=None, internal_token="", embedding_dimension=8)
    client = make_client(settings=settings)
    response = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "1", "userId": "1"},
        files={"file": ("notes.md", b"hello", "text/markdown")},
        headers=HEADERS,
    )
    assert response.status_code == 503


def test_parse_returns_chunks_and_rejects_user_mismatch() -> None:
    client = make_client()
    response = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "2", "userId": "1"},
        files={"file": ("notes.md", b"# T\n\nhello\n\nworld", "text/markdown")},
        headers=HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["parserVersion"] == "document-parser-v1"
    assert body["data"]["chunks"][0]["content"].startswith("T\n\n")
    assert body["data"]["chunks"][0]["tokenCountEstimated"] is True

    mismatch = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "2", "userId": "2"},
        files={"file": ("notes.md", b"hello", "text/markdown")},
        headers=HEADERS,
    )
    assert mismatch.status_code == 403


def test_parse_rejects_invalid_file_and_too_large() -> None:
    client = make_client()

    invalid_ext = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "2", "userId": "1"},
        files={"file": ("notes.pdf", b"hello", "application/pdf")},
        headers=HEADERS,
    )
    assert invalid_ext.status_code == 400

    invalid_utf8 = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "2", "userId": "1"},
        files={"file": ("notes.txt", b"\xff\xfe", "text/plain")},
        headers=HEADERS,
    )
    assert invalid_utf8.status_code == 400

    too_large = client.post(
        "/agent/documents/parse",
        data={"documentId": "1", "learningSpaceId": "2", "userId": "1"},
        files={"file": ("notes.txt", b"x" * 2000, "text/plain")},
        headers=HEADERS,
    )
    assert too_large.status_code == 413


def _index_payload(**overrides):
    payload = {
        "documentId": 5,
        "userId": 1,
        "spaceId": 9,
        "documentName": "notes",
        "fileName": "notes.md",
        "fileType": "md",
        "chunks": [
            {"chunkId": 11, "chunkIndex": 0, "content": "hello", "metadata": {"a": 1}},
            {"chunkId": 12, "chunkIndex": 1, "content": "world", "metadata": {}},
        ],
    }
    payload.update(overrides)
    return payload


def test_index_embeds_and_returns_point_mapping() -> None:
    store = FakeStore()
    client = make_client(store=store)

    response = client.post("/agent/documents/index", json=_index_payload(), headers=HEADERS)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["collection"] == "test_hash-v1_8"
    assert data["embeddingModel"] == "hash-v1"
    assert data["embeddingProvider"] == "hash"
    assert [item["chunkId"] for item in data["vectors"]] == [11, 12]
    assert data["vectors"][0]["pointId"] == point_id_for(5, 0)
    assert store.ensured == ("hash-v1", 8)
    assert store.indexed["document_id"] == 5
    assert store.indexed["user_id"] == 1
    assert store.indexed["space_id"] == 9
    assert [chunk.chunk_id for chunk in store.indexed["chunks"]] == [11, 12]


def test_index_requires_auth_and_matching_user() -> None:
    client = make_client()
    assert client.post("/agent/documents/index", json=_index_payload()).status_code == 401

    response = client.post(
        "/agent/documents/index",
        json=_index_payload(userId=2),
        headers=HEADERS,
    )
    assert response.status_code == 403


def test_index_rejects_duplicate_or_non_contiguous_chunks() -> None:
    client = make_client()
    duplicate = _index_payload(
        chunks=[
            {"chunkId": 11, "chunkIndex": 0, "content": "a"},
            {"chunkId": 11, "chunkIndex": 1, "content": "b"},
        ]
    )
    assert client.post("/agent/documents/index", json=duplicate, headers=HEADERS).status_code == 422

    gap = _index_payload(
        chunks=[
            {"chunkId": 11, "chunkIndex": 0, "content": "a"},
            {"chunkId": 12, "chunkIndex": 2, "content": "b"},
        ]
    )
    assert client.post("/agent/documents/index", json=gap, headers=HEADERS).status_code == 422


def test_index_reports_embedding_and_qdrant_failures() -> None:
    failing_embedding = make_client(store=FakeStore())
    failing_embedding.app.state.embedding_provider = FailingEmbedding(dimension=8)

    embedding_response = failing_embedding.post(
        "/agent/documents/index", json=_index_payload(), headers=HEADERS
    )
    assert embedding_response.status_code == 502

    qdrant_failure = make_client(store=FakeStore(fail=True))
    qdrant_response = qdrant_failure.post(
        "/agent/documents/index", json=_index_payload(), headers=HEADERS
    )
    assert qdrant_response.status_code == 502
    assert qdrant_response.json()["code"] == 502
