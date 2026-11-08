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
    memory_version: int = 1,
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
        "memory_version": memory_version,
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
                memory_version=1,
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

        # 乱序：V2 先写入，晚到的 V1 被丢弃，最终保留 V2。
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(105),
            vector=vectors[0],
            payload=_payload(
                memory_id=105,
                user_id=7,
                content_hash="h105-v2",
                fingerprint=provider.fingerprint,
                memory_version=2,
            ),
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(105),
            vector=vectors[2],
            payload=_payload(
                memory_id=105,
                user_id=7,
                content_hash="h105-v1",
                fingerprint=provider.fingerprint,
                memory_version=1,
            ),
        )
        payload = await store.retrieve_payload(
            collection_name=collection, point_id=point_id_for_memory(105)
        )
        assert payload is not None
        assert payload["content_hash"] == "h105-v2"
        assert payload["memory_version"] == 2

        # Delete 不物理删除，而是写墓碑；晚到的旧 UPSERT 不能复活数据。
        await store.delete_memory(
            collection_name=collection, memory_id=105, memory_version=3
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(105),
            vector=vectors[0],
            payload=_payload(
                memory_id=105,
                user_id=7,
                content_hash="h105-v2-late",
                fingerprint=provider.fingerprint,
                memory_version=2,
            ),
        )
        payload = await store.retrieve_payload(
            collection_name=collection, point_id=point_id_for_memory(105)
        )
        assert payload is not None
        assert payload["deleted"] is True
        assert payload["status"] == "DELETED"
        assert payload["memory_version"] == 3
        # 墓碑只改状态/版本，不覆盖原有 payload。
        assert payload["content_hash"] == "h105-v2"
        hits, _ = await store.search(
            query_vector=vectors[0],
            user_id=7,
            space_id=None,
            concept_id=None,
            top_k=10,
            model=provider.model,
            dimension=provider.dimension,
        )
        assert 105 not in {hit.memory_id for hit in hits}

        # 删除先于任何 UPSERT 到达：墓碑占位点同样挡住旧事件。
        await store.delete_memory(
            collection_name=collection, memory_id=106, memory_version=5
        )
        await store.upsert_memory(
            collection_name=collection,
            point_id=point_id_for_memory(106),
            vector=vectors[0],
            payload=_payload(
                memory_id=106,
                user_id=7,
                content_hash="h106-v4",
                fingerprint=provider.fingerprint,
                memory_version=4,
            ),
        )
        payload_106 = await store.retrieve_payload(
            collection_name=collection, point_id=point_id_for_memory(106)
        )
        assert payload_106 is not None
        assert payload_106["deleted"] is True
        assert payload_106["memory_version"] == 5

        # 重复删除幂等：墓碑保留，物理 point 数仍为 1。
        await store.delete_memory(
            collection_name=collection, memory_id=101, memory_version=2
        )
        await store.delete_memory(
            collection_name=collection, memory_id=101, memory_version=2
        )
        payload = await store.retrieve_payload(
            collection_name=collection, point_id=point_id_for_memory(101)
        )
        assert payload is not None
        assert payload["deleted"] is True
        assert payload["status"] == "DELETED"
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
    finally:
        try:
            await store._client.delete_collection(collection)
        finally:
            await store.aclose()
