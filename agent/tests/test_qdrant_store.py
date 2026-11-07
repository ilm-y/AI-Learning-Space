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
        assert field_schema in {
            qm.PayloadSchemaType.INTEGER,
            qm.PayloadSchemaType.KEYWORD,
        }
        self.payload_indexes[collection_name].add(field_name)

    async def delete(self, *, collection_name: str, points_selector, wait: bool) -> None:
        points = self.points[collection_name]
        for point_id in [
            point_id
            for point_id, point in points.items()
            if _matches(filter_obj=points_selector.filter, payload=point.payload)
        ]:
            del points[point_id]

    async def upsert(self, *, collection_name: str, points, wait: bool) -> None:
        self.upsert_calls += 1
        for point in points:
            self.points[collection_name][str(point.id)] = point

    async def count(self, *, collection_name: str, count_filter, exact: bool):
        total = sum(
            1
            for point in self.points[collection_name].values()
            if _matches(filter_obj=count_filter, payload=point.payload)
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
    for condition in filter_obj.must or []:
        if not _matches_condition(condition=condition, payload=payload):
            return False
    for condition in filter_obj.must_not or []:
        if _matches_condition(condition=condition, payload=payload):
            return False
    should = filter_obj.should or []
    return not should or any(
        _matches_condition(condition=condition, payload=payload)
        for condition in should
    )


def _matches_condition(*, condition, payload: dict) -> bool:
    if isinstance(condition, qm.IsEmptyCondition):
        return payload.get(condition.is_empty.key) is None
    value = payload.get(condition.key)
    match = condition.match
    if isinstance(match, qm.MatchAny):
        return value in match.any
    if isinstance(match, qm.MatchValue):
        return value == match.value
    raise AssertionError(f"unexpected match type: {type(match)}")  # pragma: no cover


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
        "index_version",
        "doc_version",
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
        document_versions=[(101, 0)],
        top_k=5,
    )

    assert [hit.payload["document_id"] for hit in hits] == [101]
    query_filter = fake.query_calls[-1]["filter"]
    assert query_filter.must[0].key == "user_id"
    assert query_filter.must[1].key == "space_id"
    # version=0 走迁移前兼容分支：按 document_id 过滤。
    assert query_filter.should[0].key == "document_id"
    assert query_filter.should[0].match.any == [101]


async def test_search_with_empty_allowlist_never_queries_qdrant() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)

    hits = await store.search_knowledge(
        collection_name=collection,
        query_vector=[1.0, 0.0],
        user_id=1,
        space_id=1,
        document_versions=[],
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


async def test_new_version_does_not_overwrite_old_version() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["old", "new"])

    old = await store.index_document(
        collection_name=collection,
        document_id=1,
        user_id=7,
        space_id=9,
        document_name="notes",
        file_type="md",
        chunks=[ChunkVector(1, 0, "old", vectors[0])],
        index_version=0,
    )
    new = await store.index_document(
        collection_name=collection,
        document_id=1,
        user_id=7,
        space_id=9,
        document_name="notes",
        file_type="md",
        chunks=[ChunkVector(2, 0, "new", vectors[1])],
        index_version=1,
    )

    # 两个版本可以同时存在；新版本 point ID 与旧版本不同。
    assert old[1] != new[2]
    assert await store.count_document_points(
        collection_name=collection, document_id=1, index_version=0
    ) == 1
    assert await store.count_document_points(
        collection_name=collection, document_id=1, index_version=1
    ) == 1
    assert len(fake.points[collection]) == 2


async def test_delete_versions_except_keeps_only_active_version() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["a", "b"])

    for version in (0, 1):
        await store.index_document(
            collection_name=collection,
            document_id=1,
            user_id=7,
            space_id=9,
            document_name="notes",
            file_type="md",
            chunks=[ChunkVector(version + 1, 0, f"v{version}", vectors[version])],
            index_version=version,
        )

    await store.delete_versions_except(
        collection_name=collection, document_id=1, keep_index_version=1
    )

    assert await store.count_document_points(
        collection_name=collection, document_id=1, index_version=0
    ) == 0
    assert await store.count_document_points(
        collection_name=collection, document_id=1, index_version=1
    ) == 1
    assert len(fake.points[collection]) == 1


async def test_delete_versions_only_removes_exact_versions_and_legacy_when_requested() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["legacy", "v1", "v2"])

    # 迁移前 point：没有 index_version 字段。
    legacy_id = point_id_for(1, 0)
    fake.points[collection][legacy_id] = qm.PointStruct(
        id=legacy_id,
        vector=vectors[0],
        payload={"document_id": 1, "content": "legacy"},
    )
    for version in (1, 2):
        await store.index_document(
            collection_name=collection,
            document_id=1,
            user_id=7,
            space_id=9,
            document_name="notes",
            file_type="md",
            chunks=[ChunkVector(version, 0, f"v{version}", vectors[version])],
            index_version=version,
        )

    await store.delete_versions(
        collection_name=collection,
        document_id=1,
        versions=[1],
        delete_legacy=True,
    )

    assert await store.count_document_points(
        collection_name=collection, document_id=1, index_version=1
    ) == 0
    assert await store.count_document_points(
        collection_name=collection, document_id=1, index_version=2
    ) == 1
    assert legacy_id not in fake.points[collection]
    assert len(fake.points[collection]) == 1


async def test_partial_write_raises_instead_of_reporting_success() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["a", "b"])

    real_upsert = fake.upsert

    async def lossy_upsert(*, collection_name: str, points, wait: bool) -> None:
        await real_upsert(collection_name=collection_name, points=points[:1], wait=wait)

    fake.upsert = lossy_upsert  # type: ignore[method-assign]

    with pytest.raises(DocumentVectorStoreError, match="写入不完整"):
        await store.index_document(
            collection_name=collection,
            document_id=1,
            user_id=1,
            space_id=1,
            document_name="one",
            file_type="txt",
            chunks=[
                ChunkVector(1, 0, "a", vectors[0]),
                ChunkVector(2, 1, "b", vectors[1]),
            ],
            index_version=3,
        )


async def test_versioned_search_filters_by_active_doc_version() -> None:
    store, fake = make_store()
    collection = await store.ensure_collection(model="hash-v1", dimension=2)
    provider = HashEmbeddingProvider(dimension=2)
    vectors = await provider.embed(["old", "new"])

    await store.index_document(
        collection_name=collection,
        document_id=101,
        user_id=7,
        space_id=9,
        document_name="notes",
        file_type="md",
        chunks=[ChunkVector(1, 0, "old", vectors[0])],
        index_version=1,
    )
    await store.index_document(
        collection_name=collection,
        document_id=101,
        user_id=7,
        space_id=9,
        document_name="notes",
        file_type="md",
        chunks=[ChunkVector(2, 0, "new", vectors[1])],
        index_version=2,
    )

    hits = await store.search_knowledge(
        collection_name=collection,
        query_vector=[1.0, 0.0],
        user_id=7,
        space_id=9,
        document_versions=[(101, 2)],
        top_k=5,
    )

    assert [hit.payload["content"] for hit in hits] == ["new"]
    query_filter = fake.query_calls[-1]["filter"]
    assert query_filter.should[0].key == "doc_version"
    assert query_filter.should[0].match.any == ["101:2"]
