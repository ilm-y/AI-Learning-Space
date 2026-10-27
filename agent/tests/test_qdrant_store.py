"""Qdrant 存储单元测试。

默认不连接真实 Qdrant：用内存 fake 校验确定性 ID、覆盖语义和文档范围删除。
真实集合的读写由 `test_qdrant_integration.py` 在显式环境变量下验证。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from qdrant_client.http import models as qm

from app.documents.errors import DocumentVectorStoreError
from app.embedding import HashEmbeddingProvider
from app.vectorstore.qdrant_store import (
    ChunkVector,
    QdrantDocumentStore,
    collection_name_for,
    point_id_for,
)


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}
        self.payload_indexes: dict[str, set[str]] = {}
        self.points: dict[str, dict[str, qm.PointStruct]] = {}
        self.upsert_calls = 0
        self.query_calls: list[dict] = []

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def create_collection(self, *, collection_name: str, vectors_config) -> None:
        self.collections[collection_name] = {"vectors": vectors_config}
        self.points[collection_name] = {}
        self.payload_indexes[collection_name] = set()

    async def get_collection(self, name: str):
        config = self.collections[name]
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=config["vectors"])),
            payload_schema={field: object() for field in self.payload_indexes[name]},
        )

    async def create_payload_index(
        self, *, collection_name: str, field_name: str, field_schema, wait: bool
    ) -> None:
        assert field_schema == qm.PayloadSchemaType.INTEGER
        self.payload_indexes[collection_name].add(field_name)

    async def delete(self, *, collection_name: str, points_selector, wait: bool) -> None:
        document_id = points_selector.filter.must[0].match.value
        points = self.points[collection_name]
        for point_id in [
            point_id
            for point_id, point in points.items()
            if point.payload["document_id"] == document_id
        ]:
            del points[point_id]

    async def upsert(self, *, collection_name: str, points, wait: bool) -> None:
        self.upsert_calls += 1
        for point in points:
            self.points[collection_name][str(point.id)] = point

    async def count(self, *, collection_name: str, count_filter, exact: bool):
        document_id = count_filter.must[0].match.value
        total = sum(
            1
            for point in self.points[collection_name].values()
            if point.payload["document_id"] == document_id
        )
        return SimpleNamespace(count=total)

    async def query_points(
        self,
        *,
        collection_name: str,
        query,
        query_filter,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
    ):
        self.query_calls.append(
            {
                "collection": collection_name,
                "query": query,
                "filter": query_filter,
                "limit": limit,
            }
        )
        matched = []
        for point in self.points[collection_name].values():
            if not _matches(filter_obj=query_filter, payload=point.payload):
                continue
            matched.append(
                SimpleNamespace(
                    id=point.id,
                    score=float(point.payload["document_id"]) / 1000.0,
                    payload=point.payload,
                )
            )
        return SimpleNamespace(points=matched[:limit])

    async def close(self) -> None:
        return None


def _matches(*, filter_obj, payload: dict) -> bool:
    for condition in filter_obj.must:
        value = payload.get(condition.key)
        match = condition.match
        if isinstance(match, qm.MatchAny):
            if value not in match.any:
                return False
        elif isinstance(match, qm.MatchValue):
            if value != match.value:
                return False
        else:  # pragma: no cover
            raise AssertionError(f"unexpected match type: {type(match)}")
    return True


def make_store() -> tuple[QdrantDocumentStore, FakeQdrantClient]:
    store = QdrantDocumentStore(
        url="http://127.0.0.1:6333",
        api_key="",
        timeout_seconds=5,
        collection_prefix="test_docs",
        upsert_batch_size=2,
    )
    fake = FakeQdrantClient()
    store._client = fake  # type: ignore[assignment] —— 单元测试专用注入
    return store, fake


def test_point_ids_are_deterministic_and_document_scoped() -> None:
    assert point_id_for(1, 0) == point_id_for(1, 0)
    assert point_id_for(1, 0) != point_id_for(1, 1)
    assert point_id_for(1, 0) != point_id_for(2, 0)


def test_collection_name_contains_sanitized_model_and_dimension() -> None:
    assert collection_name_for("ails_docs", "hash-v1", 256) == "ails_docs_hash-v1_256"
    name = collection_name_for("ails docs", "org/model:1", 768)
    assert "/" not in name and ":" not in name and " " not in name
    assert name.endswith("_768")


async def test_ensure_collection_creates_indexes_only_once() -> None:
    store, fake = make_store()
    name = await store.ensure_collection(model="hash-v1", dimension=8)
    await store.ensure_collection(model="hash-v1", dimension=8)

    assert name == "test_docs_hash-v1_8"
    assert fake.payload_indexes[name] == {
        "user_id",
        "space_id",
        "document_id",
        "chunk_id",
    }


async def test_ensure_collection_rejects_dimension_mismatch() -> None:
    store, fake = make_store()
    fake.collections["test_docs_hash-v1_8"] = {
        "vectors": qm.VectorParams(size=99, distance=qm.Distance.COSINE)
    }
    fake.payload_indexes["test_docs_hash-v1_8"] = set()
    fake.points["test_docs_hash-v1_8"] = {}

    with pytest.raises(DocumentVectorStoreError, match="维度不一致"):
        await store.ensure_collection(model="hash-v1", dimension=8)


async def test_reindex_updates_points_instead_of_duplicating() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=4)
    provider = HashEmbeddingProvider(dimension=4)
    vectors = await provider.embed(["one", "two"])

    chunks = [
        ChunkVector(11, 0, "one", vectors[0], {"headingPath": []}),
        ChunkVector(12, 1, "two", vectors[1], {"headingPath": []}),
    ]
    first = await store.index_document(
        collection_name=collection,
        document_id=1,
        user_id=7,
        space_id=9,
        document_name="notes",
        file_type="md",
        chunks=chunks,
    )
    second = await store.index_document(
        collection_name=collection,
        document_id=1,
        user_id=7,
        space_id=9,
        document_name="notes",
        file_type="md",
        chunks=chunks,
    )

    assert first == second
    assert len(first) == 2
    assert fake.points[collection] and len(fake.points[collection]) == 2
    for point in fake.points[collection].values():
        assert point.payload["user_id"] == 7
        assert point.payload["space_id"] == 9
        assert point.payload["document_id"] == 1
        assert point.payload["document_name"] == "notes"
        assert point.payload["file_type"] == "md"


async def test_delete_only_removes_scoped_document() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["a", "b"])

    await store.index_document(
        collection_name=collection,
        document_id=1,
        user_id=1,
        space_id=1,
        document_name="one",
        file_type="txt",
        chunks=[ChunkVector(1, 0, "a", vectors[0])],
    )
    await store.index_document(
        collection_name=collection,
        document_id=2,
        user_id=1,
        space_id=1,
        document_name="two",
        file_type="txt",
        chunks=[ChunkVector(2, 0, "b", vectors[1])],
    )

    await store.delete_document(collection_name=collection, document_id=1)

    assert await store.count_document_points(collection_name=collection, document_id=1) == 0
    assert await store.count_document_points(collection_name=collection, document_id=2) == 1


async def test_search_applies_user_space_and_document_allowlist_filters() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["a", "b", "c"])

    await store.index_document(
        collection_name=collection,
        document_id=101,
        user_id=7,
        space_id=9,
        document_name="one",
        file_type="md",
        chunks=[ChunkVector(1, 0, "a", vectors[0])],
    )
    await store.index_document(
        collection_name=collection,
        document_id=202,
        user_id=7,
        space_id=9,
        document_name="two",
        file_type="md",
        chunks=[ChunkVector(2, 0, "b", vectors[1])],
    )
    await store.index_document(
        collection_name=collection,
        document_id=303,
        user_id=8,
        space_id=9,
        document_name="other-user",
        file_type="md",
        chunks=[ChunkVector(3, 0, "c", vectors[2])],
    )

    hits = await store.search_knowledge(
        collection_name=collection,
        query_vector=[1.0, 0.0],
        user_id=7,
        space_id=9,
        document_ids=[101],
        top_k=5,
    )

    assert [hit.payload["document_id"] for hit in hits] == [101]
    assert fake.query_calls[-1]["filter"].must[0].key == "user_id"
    assert fake.query_calls[-1]["filter"].must[1].key == "space_id"
    assert fake.query_calls[-1]["filter"].must[2].key == "document_id"
    assert fake.query_calls[-1]["filter"].must[2].match.any == [101]


async def test_search_with_empty_allowlist_never_queries_qdrant() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)

    hits = await store.search_knowledge(
        collection_name=collection,
        query_vector=[1.0, 0.0],
        user_id=1,
        space_id=1,
        document_ids=[],
        top_k=5,
    )

    assert hits == []
    assert fake.query_calls == []


async def test_qdrant_write_failure_is_explicit() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)

    async def failing_upsert(**kwargs):
        raise RuntimeError("qdrant down")

    fake.upsert = failing_upsert  # type: ignore[method-assign]
    chunks = [ChunkVector(1, 0, "a", [0.1, 0.2])]

    with pytest.raises(DocumentVectorStoreError, match="Qdrant 写入失败"):
        await store.index_document(
            collection_name=collection,
            document_id=1,
            user_id=1,
            space_id=1,
            document_name="one",
            file_type="txt",
            chunks=chunks,
        )
