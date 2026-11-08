"""记忆向量存储的版本顺序单元测试。

默认不连接真实 Qdrant：用内存 fake 复现 Outbox 事件乱序的两种场景：

1. V2 先写入、V1 后写入 → 最终必须保留 V2；
2. DELETE 后跟一个迟到的旧 UPSERT → 数据不能复活。

真实 collection 上的同样场景由 `test_memory_qdrant_integration.py` 在
`AILS_QDRANT_IT=true` 下验证。
"""

from __future__ import annotations

from types import SimpleNamespace

from qdrant_client.http import models as qm

from app.vectorstore.memory_store import QdrantMemoryStore, point_id_for_memory

DIMENSION = 4
MODEL = "hash-v1"


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: dict[str, qm.VectorParams] = {}
        self.points: dict[str, dict[str, qm.PointStruct]] = {}
        self.upsert_calls = 0
        self.set_payload_calls = 0

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def create_collection(self, *, collection_name: str, vectors_config) -> None:
        self.collections[collection_name] = vectors_config
        self.points[collection_name] = {}

    async def get_collection(self, name: str):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=self.collections[name])
            ),
            payload_schema={},
        )

    async def create_payload_index(
        self, *, collection_name: str, field_name: str, field_schema, wait: bool
    ) -> None:
        return None

    async def retrieve(
        self,
        *,
        collection_name: str,
        ids,
        with_payload: bool,
        with_vectors: bool,
    ):
        records = []
        for point_id in ids:
            point = self.points[collection_name].get(str(point_id))
            if point is None:
                continue
            records.append(
                SimpleNamespace(
                    id=point.id,
                    payload=dict(point.payload or {}),
                    vector=point.vector,
                )
            )
        return records

    async def upsert(self, *, collection_name: str, points, wait: bool) -> None:
        self.upsert_calls += 1
        for point in points:
            self.points[collection_name][str(point.id)] = point

    async def set_payload(
        self, *, collection_name: str, payload: dict, points, wait: bool
    ) -> None:
        self.set_payload_calls += 1
        for point_id in points:
            point = self.points[collection_name].get(str(point_id))
            if point is None:
                continue
            merged = dict(point.payload or {})
            merged.update(payload)
            self.points[collection_name][str(point_id)] = qm.PointStruct(
                id=point.id, vector=point.vector, payload=merged
            )

    async def close(self) -> None:
        return None


def make_store() -> tuple[QdrantMemoryStore, FakeQdrantClient]:
    store = QdrantMemoryStore(
        url="http://127.0.0.1:6333",
        api_key="",
        timeout_seconds=5,
        collection_prefix="test_mem",
    )
    fake = FakeQdrantClient()
    store._client = fake  # type: ignore[assignment] —— 单元测试专用注入
    return store, fake


def _payload(memory_id: int, *, version: int, content_hash: str) -> dict:
    return {
        "memory_id": memory_id,
        "user_id": 7,
        "status": "ACTIVE",
        "scope_type": "GLOBAL",
        "space_id": None,
        "concept_id": None,
        "memory_type": "LEARNING",
        "importance": 0.5,
        "content_hash": content_hash,
        "memory_version": version,
        "embedding_fingerprint": "v1:test",
    }


async def _setup() -> tuple[QdrantMemoryStore, FakeQdrantClient, str]:
    store, fake = make_store()
    collection = await store.ensure_collection(model=MODEL, dimension=DIMENSION)
    return store, fake, collection


async def test_stale_upsert_is_discarded_so_v2_survives_v1() -> None:
    store, fake, collection = await _setup()
    point_id = point_id_for_memory(42)

    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[1.0, 0.0, 0.0, 0.0],
        payload=_payload(42, version=2, content_hash="h-v2"),
    )
    upserts_after_v2 = fake.upsert_calls

    # 迟到的 V1：必须直接丢弃，既不覆盖 payload 也不覆盖向量。
    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[0.0, 1.0, 0.0, 0.0],
        payload=_payload(42, version=1, content_hash="h-v1"),
    )

    assert fake.upsert_calls == upserts_after_v2
    stored = fake.points[collection][point_id]
    assert stored.payload["content_hash"] == "h-v2"
    assert stored.payload["memory_version"] == 2
    assert stored.vector == [1.0, 0.0, 0.0, 0.0]


async def test_delete_writes_tombstone_and_late_upsert_cannot_resurrect() -> None:
    store, fake, collection = await _setup()
    point_id = point_id_for_memory(43)

    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[1.0, 0.0, 0.0, 0.0],
        payload=_payload(43, version=2, content_hash="h-v2"),
    )
    await store.delete_memory(
        collection_name=collection, memory_id=43, memory_version=3
    )

    tombstone = fake.points[collection][point_id].payload
    assert tombstone["deleted"] is True
    assert tombstone["status"] == "DELETED"
    assert tombstone["memory_version"] == 3
    # 墓碑更新状态但保留原 payload 与向量，物理 point 未删除。
    assert tombstone["content_hash"] == "h-v2"
    assert fake.points[collection][point_id].vector == [1.0, 0.0, 0.0, 0.0]

    upserts_before = fake.upsert_calls
    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[0.0, 1.0, 0.0, 0.0],
        payload=_payload(43, version=2, content_hash="h-v2-late"),
    )

    assert fake.upsert_calls == upserts_before
    still_tombstone = fake.points[collection][point_id].payload
    assert still_tombstone["deleted"] is True
    assert still_tombstone["memory_version"] == 3
    assert still_tombstone["content_hash"] == "h-v2"


async def test_delete_before_first_upsert_creates_blocking_tombstone() -> None:
    store, fake, collection = await _setup()
    point_id = point_id_for_memory(44)

    # 删除事件先到、此前从未写入过该记忆。
    await store.delete_memory(
        collection_name=collection, memory_id=44, memory_version=5
    )
    tombstone = fake.points[collection][point_id]
    assert tombstone.payload["deleted"] is True
    assert tombstone.payload["memory_version"] == 5
    assert len(tombstone.vector) == DIMENSION

    # 随后迟到的 V4 必须被墓碑挡住。
    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[0.0, 1.0, 0.0, 0.0],
        payload=_payload(44, version=4, content_hash="h-v4"),
    )

    assert fake.points[collection][point_id].payload["deleted"] is True
    assert fake.points[collection][point_id].payload["memory_version"] == 5


async def test_repeated_delete_keeps_tombstone_idempotent() -> None:
    store, fake, collection = await _setup()
    point_id = point_id_for_memory(45)

    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[1.0, 0.0, 0.0, 0.0],
        payload=_payload(45, version=1, content_hash="h-v1"),
    )
    await store.delete_memory(
        collection_name=collection, memory_id=45, memory_version=2
    )
    await store.delete_memory(
        collection_name=collection, memory_id=45, memory_version=2
    )

    assert len(fake.points[collection]) == 1
    assert fake.points[collection][point_id].payload["deleted"] is True
    assert fake.points[collection][point_id].payload["memory_version"] == 2


async def test_stale_delete_does_not_lower_tombstone_version() -> None:
    store, fake, collection = await _setup()
    point_id = point_id_for_memory(47)

    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[1.0, 0.0, 0.0, 0.0],
        payload=_payload(47, version=2, content_hash="h-v2"),
    )
    await store.delete_memory(
        collection_name=collection, memory_id=47, memory_version=4
    )
    # 迟到的旧 DELETE 不能把墓碑版本从 4 降回 2。
    await store.delete_memory(
        collection_name=collection, memory_id=47, memory_version=2
    )

    assert fake.points[collection][point_id].payload["memory_version"] == 4

    # 因此 V3 的 UPSERT 仍然会被墓碑挡住。
    await store.upsert_memory(
        collection_name=collection,
        point_id=point_id,
        vector=[0.0, 1.0, 0.0, 0.0],
        payload=_payload(47, version=3, content_hash="h-v3"),
    )
    assert fake.points[collection][point_id].payload["deleted"] is True
    assert fake.points[collection][point_id].payload["memory_version"] == 4


async def test_delete_on_missing_collection_does_not_create_it() -> None:
    store, fake = make_store()

    await store.delete_memory(
        collection_name="test_mem_hash-v1_4", memory_id=46, memory_version=9
    )

    assert fake.collections == {}
    assert fake.upsert_calls == 0
