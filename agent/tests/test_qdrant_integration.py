"""真实 Qdrant 集成测试（默认跳过）。

启用方式：

```bash
AILS_QDRANT_IT=true AILS_QDRANT_URL=http://127.0.0.1:6333 \
  python -m pytest tests/test_qdrant_integration.py -v
```

测试使用带随机后缀的临时 collection，结束后删除，不会碰已有 collection。
Embedding 使用离线 Hash，因此验证的是「Qdrant 存储语义」，不是语义检索质量。
"""

from __future__ import annotations

import os
import uuid

import pytest
from qdrant_client.http import models as qm

from app.embedding import HashEmbeddingProvider
from app.vectorstore.qdrant_store import (
    ChunkVector,
    QdrantDocumentStore,
    collection_name_for,
    point_id_for,
)

pytestmark = pytest.mark.skipif(
    os.getenv("AILS_QDRANT_IT", "").lower() != "true",
    reason="设置 AILS_QDRANT_IT=true 才运行真实 Qdrant 集成测试",
)

URL = os.getenv("AILS_QDRANT_URL", "http://127.0.0.1:6333")


async def test_real_collection_index_reindex_and_document_scoped_delete() -> None:
    prefix = f"ails_it_{uuid.uuid4().hex[:10]}"
    store = QdrantDocumentStore(
        url=URL,
        api_key=os.getenv("AILS_QDRANT_API_KEY", ""),
        timeout_seconds=10,
        collection_prefix=prefix,
        upsert_batch_size=2,
    )
    provider = HashEmbeddingProvider(dimension=12)
    collection = collection_name_for(prefix, provider.model, provider.dimension)

    try:
        name = await store.ensure_collection(model=provider.model, dimension=provider.dimension)
        assert name == collection

        info = await store._client.get_collection(collection)
        assert set(info.payload_schema) >= {
            "user_id",
            "space_id",
            "document_id",
            "chunk_id",
            "index_version",
            "doc_version",
        }

        vectors = await provider.embed(["alpha", "beta", "gamma"])
        await store.index_document(
            collection_name=collection,
            document_id=101,
            user_id=7,
            space_id=9,
            document_name="notes",
            file_type="md",
            chunks=[
                ChunkVector(1001, 0, "alpha", vectors[0]),
                ChunkVector(1002, 1, "beta", vectors[1]),
            ],
        )
        await store.index_document(
            collection_name=collection,
            document_id=202,
            user_id=7,
            space_id=9,
            document_name="other",
            file_type="txt",
            chunks=[ChunkVector(2001, 0, "gamma", vectors[2])],
        )

        assert await store.count_document_points(collection_name=collection, document_id=101) == 2
        assert await store.count_document_points(collection_name=collection, document_id=202) == 1

        # 第二次索引同一文档：确定性 ID 覆盖旧 points，数量不增长。
        await store.index_document(
            collection_name=collection,
            document_id=101,
            user_id=7,
            space_id=9,
            document_name="notes",
            file_type="md",
            chunks=[
                ChunkVector(1001, 0, "alpha", vectors[0]),
                ChunkVector(1002, 1, "beta", vectors[1]),
            ],
        )
        assert await store.count_document_points(collection_name=collection, document_id=101) == 2

        points = await store._client.retrieve(
            collection_name=collection,
            ids=[point_id_for(101, 0), point_id_for(101, 1)],
            with_payload=True,
        )
        assert {point.payload["chunk_id"] for point in points} == {1001, 1002}
        assert all(point.payload["space_id"] == 9 for point in points)

        await store.delete_document(collection_name=collection, document_id=101)
        assert await store.count_document_points(collection_name=collection, document_id=101) == 0
        assert await store.count_document_points(collection_name=collection, document_id=202) == 1
    finally:
        try:
            await store._client.delete_collection(collection)
        finally:
            await store.aclose()


async def test_real_staged_version_switch_and_cleanup() -> None:
    prefix = f"ails_it_{uuid.uuid4().hex[:10]}"
    store = QdrantDocumentStore(
        url=URL,
        api_key=os.getenv("AILS_QDRANT_API_KEY", ""),
        timeout_seconds=10,
        collection_prefix=prefix,
        upsert_batch_size=2,
    )
    provider = HashEmbeddingProvider(dimension=4)
    collection = collection_name_for(prefix, provider.model, provider.dimension)

    try:
        await store.ensure_collection(model=provider.model, dimension=provider.dimension)
        vectors = await provider.embed(["old", "new"])

        # 版本 0 是旧活跃索引；版本 1 是构建中的新索引。
        await store.index_document(
            collection_name=collection,
            document_id=101,
            user_id=7,
            space_id=9,
            document_name="notes",
            file_type="md",
            chunks=[ChunkVector(1001, 0, "old", vectors[0])],
            index_version=0,
        )
        await store.index_document(
            collection_name=collection,
            document_id=101,
            user_id=7,
            space_id=9,
            document_name="notes",
            file_type="md",
            chunks=[ChunkVector(1002, 0, "new", vectors[1])],
            index_version=1,
        )

        # 两个版本同时存在，旧版本仍然可服务。
        assert await store.count_document_points(
            collection_name=collection, document_id=101, index_version=0
        ) == 1
        assert await store.count_document_points(
            collection_name=collection, document_id=101, index_version=1
        ) == 1

        # 切换活跃版本后检索只命中新版本。
        hits = await store.search_knowledge(
            collection_name=collection,
            query_vector=vectors[1],
            user_id=7,
            space_id=9,
            document_versions=[(101, 1)],
            top_k=5,
        )
        assert [hit.payload["content"] for hit in hits] == ["new"]

        # 清理旧版本；活跃版本保持不变。
        await store.delete_versions_except(
            collection_name=collection, document_id=101, keep_index_version=1
        )
        assert await store.count_document_points(
            collection_name=collection, document_id=101, index_version=0
        ) == 0
        assert await store.count_document_points(
            collection_name=collection, document_id=101, index_version=1
        ) == 1
    finally:
        try:
            await store._client.delete_collection(collection)
        finally:
            await store.aclose()


async def test_real_exact_version_delete_preserves_other_versions() -> None:
    prefix = f"ails_it_{uuid.uuid4().hex[:10]}"
    store = QdrantDocumentStore(
        url=URL,
        api_key=os.getenv("AILS_QDRANT_API_KEY", ""),
        timeout_seconds=10,
        collection_prefix=prefix,
        upsert_batch_size=2,
    )
    provider = HashEmbeddingProvider(dimension=3)
    collection = collection_name_for(prefix, provider.model, provider.dimension)

    try:
        await store.ensure_collection(model=provider.model, dimension=provider.dimension)
        vectors = await provider.embed(["legacy", "v1", "v2"])

        # 模拟迁移前 point：没有 index_version 字段。
        legacy_id = point_id_for(101, 0)
        await store._client.upsert(
            collection_name=collection,
            points=[
                qm.PointStruct(
                    id=legacy_id,
                    vector=vectors[0],
                    payload={"document_id": 101, "content": "legacy"},
                )
            ],
            wait=True,
        )
        for version in (1, 2):
            await store.index_document(
                collection_name=collection,
                document_id=101,
                user_id=7,
                space_id=9,
                document_name="notes",
                file_type="md",
                chunks=[ChunkVector(version, 0, f"v{version}", vectors[version])],
                index_version=version,
            )

        # 只删 v1 + legacy；v2 必须保留。
        await store.delete_versions(
            collection_name=collection,
            document_id=101,
            versions=[1],
            delete_legacy=True,
        )

        assert await store.count_document_points(
            collection_name=collection, document_id=101, index_version=1
        ) == 0
        assert await store.count_document_points(
            collection_name=collection, document_id=101, index_version=2
        ) == 1
        assert await store.count_document_points(
            collection_name=collection, document_id=101
        ) == 1
    finally:
        try:
            await store._client.delete_collection(collection)
        finally:
            await store.aclose()


async def test_real_knowledge_search_enforces_permissions_and_ranking() -> None:
    prefix = f"ails_it_{uuid.uuid4().hex[:10]}"
    store = QdrantDocumentStore(
        url=URL,
        api_key=os.getenv("AILS_QDRANT_API_KEY", ""),
        timeout_seconds=10,
        collection_prefix=prefix,
        upsert_batch_size=10,
    )
    provider = HashEmbeddingProvider(dimension=3)
    collection = collection_name_for(prefix, provider.model, provider.dimension)

    async def index(document_id, user_id, space_id, name, vector, chunk_id):
        await store.index_document(
            collection_name=collection,
            document_id=document_id,
            user_id=user_id,
            space_id=space_id,
            document_name=name,
            file_type="md",
            chunks=[ChunkVector(chunk_id, 0, name, vector)],
        )

    try:
        await store.ensure_collection(model=provider.model, dimension=provider.dimension)

        # 与查询向量完全同向 → 1.0；正交 → 0.0；反向 → -1.0
        await index(101, 7, 9, "related", [1.0, 0.0, 0.0], 1001)
        await index(202, 7, 9, "unrelated", [0.0, 1.0, 0.0], 2001)
        # 以下三条即使内容和向量都与查询相同，也不得越过权限边界：
        await index(303, 8, 9, "other-user", [1.0, 0.0, 0.0], 3001)
        await index(404, 7, 10, "other-space", [1.0, 0.0, 0.0], 4001)
        await index(505, 7, 9, "deleted-in-mysql", [1.0, 0.0, 0.0], 5001)

        hits = await store.search_knowledge(
            collection_name=collection,
            query_vector=[1.0, 0.0, 0.0],
            user_id=7,
            space_id=9,
            document_versions=[(101, 0), (202, 0)],
            top_k=5,
        )

        # 白名单内只返回两条，且相关片段排在无关片段之前。
        assert [hit.payload["document_id"] for hit in hits] == [101, 202]
        assert hits[0].score > hits[1].score
        assert hits[0].payload["chunk_id"] == 1001
        assert hits[0].payload["chunk_index"] == 0
        assert hits[0].payload["file_name"] == "related"
        assert hits[0].payload["file_type"] == "md"

        # 其他用户 / 其他 Space 的文档即使显式传入白名单也检索不到。
        assert await store.search_knowledge(
            collection_name=collection,
            query_vector=[1.0, 0.0, 0.0],
            user_id=7,
            space_id=9,
            document_versions=[(303, 0)],
            top_k=5,
        ) == []
        assert await store.search_knowledge(
            collection_name=collection,
            query_vector=[1.0, 0.0, 0.0],
            user_id=7,
            space_id=9,
            document_versions=[(404, 0)],
            top_k=5,
        ) == []

        # Qdrant 中仍有 505 的残留向量，但 MySQL 白名单不再包含它 → 不返回。
        hits_without_505 = await store.search_knowledge(
            collection_name=collection,
            query_vector=[1.0, 0.0, 0.0],
            user_id=7,
            space_id=9,
            document_versions=[(101, 0)],
            top_k=5,
        )
        assert [hit.payload["document_id"] for hit in hits_without_505] == [101]
    finally:
        try:
            await store._client.delete_collection(collection)
        finally:
            await store.aclose()
