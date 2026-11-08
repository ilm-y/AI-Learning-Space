"""Qdrant 记忆向量存储。

与文档集合共享命名与校验规则，但**刻意使用独立 collection 前缀**
（`ails_memories`），避免记忆向量与文档 chunk 被同一次知识检索召回。

关键约束：

* collection 名 = `<prefix>_<model>_<dimension>`；Hash 与真实语义模型不混用；
* point id 由 `memory_id` 通过 UUIDv5 确定性生成，重试是覆盖而不是追加；
* payload 不写 `content`：检索只返回 id / 分数，正文必须由 Spring 回 MySQL 回源；
* 检索必须同时过滤 `user_id`、`status=ACTIVE` 与 `embedding_fingerprint`；
  `space_id` 为空时不做作用域过滤，与 `MemoryService.search` 的语义保持一致；
* 删除 **不做物理删除**，而是把 point 改写成 tombstone（`status=DELETED`）：
  Outbox 事件可能因网络重试乱序到达，墓碑让晚到的旧 UPSERT 无法复活已删除记忆；
* 写入前比较 `memory_version`，incoming 落后于现有版本时丢弃，避免 V1 覆盖 V2。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from app.documents.errors import DocumentVectorStoreError
from app.memories.errors import (
    MemoryVectorStoreError,
    MemoryVectorStoreUnavailable,
)
from app.vectorstore.qdrant_store import (
    assert_vector_config,
    collection_name_for,
    ensure_payload_indexes,
)

log = logging.getLogger(__name__)

_MEMORY_POINT_NAMESPACE = uuid.UUID("5d2f8c41-9a3b-4e7c-8f60-2b7d1c4a9e80")
# 墓碑状态：检索只认 ACTIVE，因此墓碑永远不会被召回；保留 point 是为了
# 记录删除时的版本号，阻止乱序到达的旧 UPSERT 把数据写回来。
_DELETED_STATUS = "DELETED"
_PAYLOAD_INDEX_FIELDS: dict[str, qm.PayloadSchemaType] = {
    "memory_id": qm.PayloadSchemaType.INTEGER,
    "user_id": qm.PayloadSchemaType.INTEGER,
    "status": qm.PayloadSchemaType.KEYWORD,
    "scope_type": qm.PayloadSchemaType.KEYWORD,
    "space_id": qm.PayloadSchemaType.INTEGER,
    "concept_id": qm.PayloadSchemaType.INTEGER,
    "embedding_fingerprint": qm.PayloadSchemaType.KEYWORD,
}


def point_id_for_memory(memory_id: int) -> str:
    """确定性 point id：同一记忆永远落在同一个点，upsert 天然幂等。"""
    if memory_id <= 0:
        raise ValueError("memory_id 必须为正整数")
    return str(uuid.uuid5(_MEMORY_POINT_NAMESPACE, f"ails-memory:{memory_id}"))


def _version_of(value: Any) -> int | None:
    """只接受真正的 int；bool / 字符串等一律视为「未知版本」。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def should_discard_upsert(
    incoming_version: int | None, existing: dict[str, Any] | None
) -> bool:
    """判断一次 UPSERT 是否应因版本落后而丢弃。

    规则：

    * 普通 point：incoming < existing 时丢弃；相等视为幂等重放，允许覆盖；
    * 墓碑：删除优先。incoming 必须**严格大于**墓碑版本才可能复活，且墓碑
      没有可比较版本时一律拒绝（归档是终态，不存在合法的后续 UPSERT）。
    """
    if not existing:
        return False

    existing_version = _version_of(existing.get("memory_version"))
    if existing.get("deleted") is True:
        if existing_version is None or incoming_version is None:
            return True
        return incoming_version <= existing_version

    if existing_version is None or incoming_version is None:
        # 无法比较版本时保持历史行为（后写胜出），不能凭空拒绝合法写入。
        return False
    return incoming_version < existing_version


@dataclass(frozen=True)
class MemoryVectorHit:
    """Qdrant 返回的一条记忆候选。

    `embedding_fingerprint` 只在服务内部用于剔除不同向量空间的候选，
    不返回给 Spring。
    """

    memory_id: int
    score: float
    content_hash: str | None = None
    embedding_fingerprint: str | None = None


@dataclass
class _LockEntry:
    """按 memory_id 串行化写入/删除，避免进程内的 read-check-write 互相穿插。"""

    lock: asyncio.Lock
    refs: int


class QdrantMemoryStore:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: float,
        collection_prefix: str,
    ) -> None:
        if not url.strip():
            raise ValueError("Qdrant URL 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("Qdrant timeout 必须为正数")
        if not collection_prefix.strip():
            raise ValueError("记忆 collection 前缀不能为空")

        self._prefix = collection_prefix.strip()
        self._client = AsyncQdrantClient(
            url=url,
            api_key=api_key or None,
            timeout=max(1, int(timeout_seconds)),
            prefer_grpc=False,
            check_compatibility=False,
        )
        self._ensure_lock = asyncio.Lock()
        # 同一 memory 的 UPSERT / DELETE 在进程内串行执行，使
        # 「读取现有版本 → 比较 → 写入」对并发请求而言是原子的。
        self._point_locks: dict[int, _LockEntry] = {}
        self._point_locks_guard = asyncio.Lock()

    # ===================== collection =====================

    def collection_name(self, *, model: str, dimension: int) -> str:
        return collection_name_for(self._prefix, model, dimension)

    async def ensure_collection(self, *, model: str, dimension: int) -> str:
        name = self.collection_name(model=model, dimension=dimension)
        async with self._ensure_lock:
            try:
                exists = await self._client.collection_exists(name)
                if not exists:
                    await self._client.create_collection(
                        collection_name=name,
                        vectors_config=qm.VectorParams(
                            size=dimension,
                            distance=qm.Distance.COSINE,
                        ),
                    )
                    log.info("Created memory collection=%s, dimension=%s", name, dimension)
                info = await self._client.get_collection(name)
                assert_vector_config(name, info, dimension)
                await ensure_payload_indexes(
                    self._client, name, info, _PAYLOAD_INDEX_FIELDS
                )
            except DocumentVectorStoreError as exc:
                raise MemoryVectorStoreError(str(exc)) from exc
            except MemoryVectorStoreError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise MemoryVectorStoreUnavailable(
                    f"Qdrant 记忆集合初始化失败：{exc}"
                ) from exc
        return name

    async def collection_exists(self, *, model: str, dimension: int) -> bool:
        name = self.collection_name(model=model, dimension=dimension)
        return await self._collection_exists(name)

    # ===================== 写入 / 删除 =====================

    async def upsert_memory(
        self,
        *,
        collection_name: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        if not vector:
            raise MemoryVectorStoreError("记忆向量不能为空")
        memory_id = payload.get("memory_id")
        if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0:
            raise MemoryVectorStoreError("记忆 payload 缺少合法的 memory_id")

        try:
            async with self._memory_lock(memory_id):
                existing = await self.retrieve_payload(
                    collection_name=collection_name, point_id=point_id
                )
                incoming_version = _version_of(payload.get("memory_version"))
                if should_discard_upsert(incoming_version, existing):
                    log.warning(
                        "丢弃过期的记忆向量写入 memoryId=%s incomingVersion=%s "
                        "existingVersion=%s collection=%s",
                        memory_id,
                        incoming_version,
                        None if existing is None else existing.get("memory_version"),
                        collection_name,
                    )
                    return
                await self._client.upsert(
                    collection_name=collection_name,
                    points=[
                        qm.PointStruct(id=point_id, vector=vector, payload=payload)
                    ],
                    wait=True,
                )
        except MemoryVectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆写入失败：{type(exc).__name__}"
            ) from exc

    async def delete_memory(
        self,
        *,
        collection_name: str,
        memory_id: int,
        memory_version: int | None = None,
    ) -> None:
        """写入墓碑而不是物理删除，保证乱序旧 UPSERT 不会复活已删除记忆。

        * point 存在：把 `status` 改成 `DELETED` 并记录删除版本，向量保留但检索
          （强制 `status=ACTIVE`）永远不会命中；
        * point 不存在：写入一个只含墓碑 payload 的占位 point。否则删除事件先到、
          旧 UPSERT 后到时，Qdrant 里没有任何版本信息可以拒绝它；
        * collection 不存在：视为成功（没有向量可保护，也不为此新建空集合）。
        """
        try:
            if not await self._collection_exists(collection_name):
                log.info("删除记忆跳过：collection %s 不存在", collection_name)
                return

            async with self._memory_lock(memory_id):
                point_id = point_id_for_memory(memory_id)
                existing = await self.retrieve_payload(
                    collection_name=collection_name, point_id=point_id
                )
                existing_version = (
                    _version_of(existing.get("memory_version"))
                    if existing is not None
                    else None
                )
                # 墓碑版本只能前进不能后退：迟到的旧 DELETE 不能把已记录的
                # 删除版本降下来，否则中间版本的 UPSERT 就能复活数据。
                versions = [
                    version
                    for version in (memory_version, existing_version)
                    if version is not None
                ]
                tombstone_version = max(versions) if versions else None
                tombstone = {
                    "memory_id": memory_id,
                    "status": _DELETED_STATUS,
                    "deleted": True,
                    "memory_version": tombstone_version,
                }

                if existing is None:
                    # 没有现成 point 就创建一个占位点：payload 是墓碑，向量只需
                    # 满足 collection 维度，检索永远不会返回它（status != ACTIVE）。
                    dimension = await self._collection_dimension(collection_name)
                    vector = [0.0] * dimension
                    vector[0] = 1.0
                    await self._client.upsert(
                        collection_name=collection_name,
                        points=[
                            qm.PointStruct(
                                id=point_id, vector=vector, payload=tombstone
                            )
                        ],
                        wait=True,
                    )
                else:
                    await self._client.set_payload(
                        collection_name=collection_name,
                        payload=tombstone,
                        points=[point_id],
                        wait=True,
                    )

                log.info(
                    "Memory 向量已写入墓碑 memoryId=%s version=%s collection=%s",
                    memory_id,
                    tombstone_version,
                    collection_name,
                )
        except MemoryVectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆删除失败：{type(exc).__name__}"
            ) from exc

    async def retrieve_payload(
        self, *, collection_name: str, point_id: str
    ) -> dict[str, Any] | None:
        """读取单个 point 的 payload，用于「内容未变则跳过重嵌入」。"""
        try:
            if not await self._collection_exists(collection_name):
                return None
            points = await self._client.retrieve(
                collection_name=collection_name,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                return None
            return dict(points[0].payload or {})
        except MemoryVectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆读取失败：{type(exc).__name__}"
            ) from exc

    # ===================== 检索 =====================

    async def search(
        self,
        *,
        query_vector: list[float],
        user_id: int,
        space_id: int | None,
        concept_id: int | None,
        top_k: int,
        model: str,
        dimension: int,
        embedding_fingerprint: str,
    ) -> tuple[list[MemoryVectorHit], list[str]]:
        """按用户 + 状态 + 指纹 + 作用域过滤后做向量检索。

        指纹必须在 Qdrant 层过滤：旧 Endpoint 的向量属于另一个向量空间，
        若在拿到 Top-K 之后再剔除，它们会先挤占名额，导致正确候选根本进不来。

        返回 `(hits, warnings)`：collection 不存在时返回空列表 + 一条 warning，
        **不在检索路径里隐式建集合**（那会掩盖写入失败）。
        """
        if top_k <= 0:
            raise MemoryVectorStoreError("记忆检索 top_k 必须为正数")
        if not query_vector:
            raise MemoryVectorStoreError("记忆检索查询向量为空")
        if not embedding_fingerprint.strip():
            raise MemoryVectorStoreError("记忆检索 embedding_fingerprint 不能为空")

        collection = self.collection_name(model=model, dimension=dimension)
        if not await self._collection_exists(collection):
            return [], ["memory collection 未建立"]

        must: list[qm.Condition] = [
            qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
            qm.FieldCondition(key="status", match=qm.MatchValue(value="ACTIVE")),
            qm.FieldCondition(
                key="embedding_fingerprint",
                match=qm.MatchValue(value=embedding_fingerprint),
            ),
        ]
        should: list[qm.Condition] | None = None
        if space_id is not None:
            should = [
                qm.FieldCondition(
                    key="scope_type", match=qm.MatchValue(value="GLOBAL")
                ),
                qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="scope_type", match=qm.MatchValue(value="SPACE")
                        ),
                        qm.FieldCondition(
                            key="space_id", match=qm.MatchValue(value=space_id)
                        ),
                    ]
                ),
            ]
            if concept_id is not None:
                should.append(
                    qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="scope_type",
                                match=qm.MatchValue(value="CONCEPT"),
                            ),
                            qm.FieldCondition(
                                key="space_id", match=qm.MatchValue(value=space_id)
                            ),
                            qm.FieldCondition(
                                key="concept_id",
                                match=qm.MatchValue(value=concept_id),
                            ),
                        ]
                    )
                )

        # should=None 表示不过滤；空列表在不同客户端版本里语义不一致，必须显式区分。
        query_filter = qm.Filter(must=must, should=should)
        try:
            result = await self._client.query_points(
                collection_name=collection,
                query=query_vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=["memory_id", "content_hash", "embedding_fingerprint"],
                with_vectors=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆检索失败：{type(exc).__name__}"
            ) from exc

        hits: list[MemoryVectorHit] = []
        for point in result.points:
            score = getattr(point, "score", None)
            payload = getattr(point, "payload", None) or {}
            memory_id = payload.get("memory_id")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                continue
            if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0:
                log.warning("丢弃缺少合法 memory_id 的记忆向量 point=%s", point.id)
                continue
            content_hash = payload.get("content_hash")
            fingerprint = payload.get("embedding_fingerprint")
            hits.append(
                MemoryVectorHit(
                    memory_id=memory_id,
                    score=float(score),
                    content_hash=content_hash if isinstance(content_hash, str) else None,
                    embedding_fingerprint=(
                        fingerprint if isinstance(fingerprint, str) else None
                    ),
                )
            )
        return hits, []

    # ---------- 内部 ----------

    @asynccontextmanager
    async def _memory_lock(self, memory_id: int) -> AsyncIterator[None]:
        """按 memory_id 加进程内互斥锁；最后一个持有者释放后清理条目。"""
        async with self._point_locks_guard:
            entry = self._point_locks.get(memory_id)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock(), refs=0)
                self._point_locks[memory_id] = entry
            entry.refs += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._point_locks_guard:
                entry.refs -= 1
                if entry.refs == 0:
                    self._point_locks.pop(memory_id, None)

    async def _collection_dimension(self, name: str) -> int:
        """读取 collection 的向量维度，仅用于给不存在的 point 写墓碑占位点。"""
        info = await self._client.get_collection(name)
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            if len(vectors) != 1:
                raise MemoryVectorStoreError(
                    f"Qdrant collection {name} 包含多个命名向量，无法写入墓碑"
                )
            vectors = next(iter(vectors.values()))
        size = getattr(vectors, "size", None)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise MemoryVectorStoreError(
                f"Qdrant collection {name} 向量维度非法：{size}"
            )
        return size

    async def _collection_exists(self, name: str) -> bool:
        try:
            return bool(await self._client.collection_exists(name))
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆 collection 状态查询失败：{type(exc).__name__}"
            ) from exc

    async def aclose(self) -> None:
        await self._client.close()
