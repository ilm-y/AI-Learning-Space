"""真实 Qdrant 的记忆向量集成测试（默认跳过）。

启用方式：

```bash
AILS_QDRANT_IT=true AILS_QDRANT_URL=http://127.0.0.1:6333 \
  python -m pytest tests/test_memory_qdrant_integration.py -v
```

使用带随机后缀的临时 collection，结束后删除，不会碰已有 collection。
Embedding 使用离线 Hash，因此验证的是 Qdrant 存储、过滤与删除语义，
不是语义检索质量。
"""

from __future__ import annotations

import os
import uuid

import pytest
from qdrant_client.http import models as qm

from app.embedding import HashEmbeddingProvider
from app.vectorstore.memory_store import (
    QdrantMemoryStore,
    point_id_for_memory,
)

pytestmark = pytest.mark.skipif(
    os.getenv("AILS_QDRANT_IT", "").lower() != "true",
    reason="设置 AILS_QDRANT_IT=true 才运行真实 Qdrant 集成测试",
)

URL = os.getenv("AILS_QDRANT_URL", "http://127.0.0.1:6333")


def _payload(
    *,
    memory_id: int,
    user_id: int,
    content_hash: str,
    fingerprint: str,
    status: str = "ACTIVE",
    scope_type: str = "GLOBAL",
    space_id: int | None = None,
    concept_id: int | None = None,
) -> dict:
    return {
        "memory_id": memory_id,
        "user_id": user_id,
        "status": status,
        "scope_type": scope_type,
        "space_id": space_id,
        "concept_id": concept_id,
        "memory_type": "LEARNING",
        "importance": 0.5,
        "content_hash": content_hash,
        "memory_version": 1,
        "embedding_fingerprint": fingerprint,
    }


async def test_real_memory_upsert_search_delete_and_isolation() -> None:
    prefix = f"ails_mem_it_{uuid.uuid4().hex[:10]}"
    store = QdrantMemoryStore(
        url=URL,
        api_key=os.getenv("AILS_QDRANT_API_KEY", ""),
        timeout_seconds=10,
        collection_prefix=prefix,
    )
    provider = HashEmbeddingProvider(dimension=12)
    collection = store.collection_name(
        model=provider.model, dimension=provider.dimension
    )

    try:
        name = await store.ensure_collection(
            model=provider.model, dimension=provider.dimension
        )
        assert name == collection
        info = await store._client.get_collection(collection)
        assert set(info.payload_schema) >= {
            "memory_id",
            "user_id",
            "status",
            "scope_type",
            "space_id",
            "concept_id",
        }

        vectors = await provider.embed(
            ["全局偏好", "空间内偏好", "别人的偏好", "归档的偏好"]
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(101),
            vector=vectors[0],
            payload=_payload(
                memory_id=101,
                user_id=7,
                content_hash="h101",
                fingerprint=provider.fingerprint,
            ),
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(102),
            vector=vectors[1],
            payload=_payload(
                memory_id=102,
                user_id=7,
                content_hash="h102",
                fingerprint=provider.fingerprint,
                scope_type="SPACE",
                space_id=9,
            ),
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(103),
            vector=vectors[2],
            payload=_payload(
                memory_id=103,
                user_id=8,
                content_hash="h103",
                fingerprint=provider.fingerprint,
            ),
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(104),
            vector=vectors[3],
            payload=_payload(
                memory_id=104,
                user_id=7,
                content_hash="h104",
                fingerprint=provider.fingerprint,
                status="ARCHIVED",
            ),
        )

        # 不带 space：该用户全部 ACTIVE（101/102），ARCHIVED 与别人的都排除。
        hits, warnings = await store.search(
            query_vector=vectors[0],
            user_id=7,
            space_id=None,
            concept_id=None,
            top_k=10,
            model=provider.model,
            dimension=provider.dimension,
        )
        assert warnings == []
        assert {hit.memory_id for hit in hits} == {101, 102}
        assert {hit.content_hash for hit in hits} == {"h101", "h102"}

        # space=9：GLOBAL 101 + SPACE 102。
        hits, _ = await store.search(
            query_vector=vectors[0],
            user_id=7,
            space_id=9,
            concept_id=None,
            top_k=10,
            model=provider.model,
            dimension=provider.dimension,
        )
        assert {hit.memory_id for hit in hits} == {101, 102}

        # 另一个空间只能看到 GLOBAL。
        hits, _ = await store.search(
            query_vector=vectors[0],
            user_id=7,
            space_id=10,
            concept_id=None,
            top_k=10,
            model=provider.model,
            dimension=provider.dimension,
        )
        assert {hit.memory_id for hit in hits} == {101}

        # 用户隔离。
        hits, _ = await store.search(
            query_vector=vectors[2],
            user_id=8,
            space_id=None,
            concept_id=None,
            top_k=10,
            model=provider.model,
            dimension=provider.dimension,
        )
        assert {hit.memory_id for hit in hits} == {103}

        # 同一个 memory 重新 upsert 是覆盖，不产生第二个点。
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(101),
            vector=vectors[0],
            payload=_payload(
                memory_id=101,
                user_id=7,
                content_hash="h101-v2",
                fingerprint=provider.fingerprint,
            ),
        )
        payload = await store.retrieve_payload(
            collection_name=collection, point_id=point_id_for_memory(101)
        )
        assert payload is not None
        assert payload["content_hash"] == "h101-v2"
        count = await store._client.count(
            collection_name=collection,
            count_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="memory_id", match=qm.MatchValue(value=101)
                    )
                ]
            ),
            exact=True,
        )
        assert count.count == 1

        # 删除按 memory_id，重复删除幂等。
        await store.delete_memory(collection_name=collection, memory_id=101)
        await store.delete_memory(collection_name=collection, memory_id=101)
        assert (
            await store.retrieve_payload(
                collection_name=collection, point_id=point_id_for_memory(101)
            )
            is None
        )
    finally:
        try:
            await store._client.delete_collection(collection)
        finally:
            await store.aclose()
