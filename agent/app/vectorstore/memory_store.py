"""Qdrant 记忆向量存储。

与文档集合共享命名与校验规则，但**刻意使用独立 collection 前缀**
（`ails_memories`），避免记忆向量与文档 chunk 被同一次知识检索召回。

关键约束：

* collection 名 = `<prefix>_<model>_<dimension>`；Hash 与真实语义模型不混用；
* point id 由 `memory_id` 通过 UUIDv5 确定性生成，重试是覆盖而不是追加；
* payload 不写 `content`：检索只返回 id / 分数，正文必须由 Spring 回 MySQL 回源；
* 检索必须同时过滤 `user_id` 与 `status=ACTIVE`；`space_id` 为空时不做作用域过滤，
  与 `MemoryService.search` 的语义保持一致；
* 删除按 `memory_id` 过滤且 `wait=True`，保证 Relay 收到成功时数据已落盘。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

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
_PAYLOAD_INDEX_FIELDS: dict[str, qm.PayloadSchemaType] = {
    "memory_id": qm.PayloadSchemaType.INTEGER,
    "user_id": qm.PayloadSchemaType.INTEGER,
    "status": qm.PayloadSchemaType.KEYWORD,
    "scope_type": qm.PayloadSchemaType.KEYWORD,
    "space_id": qm.PayloadSchemaType.INTEGER,
    "concept_id": qm.PayloadSchemaType.INTEGER,
}


def point_id_for_memory(memory_id: int) -> str:
    """确定性 point id：同一记忆永远落在同一个点，upsert 天然幂等。"""
    if memory_id <= 0:
        raise ValueError("memory_id 必须为正整数")
    return str(uuid.uuid5(_MEMORY_POINT_NAMESPACE, f"ails-memory:{memory_id}"))


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
        if not payload.get("memory_id"):
            raise MemoryVectorStoreError("记忆 payload 缺少 memory_id")
        try:
            await self._client.upsert(
                collection_name=collection_name,
                points=[
                    qm.PointStruct(id=point_id, vector=vector, payload=payload)
                ],
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆写入失败：{type(exc).__name__}"
            ) from exc

    async def delete_memory(self, *, collection_name: str, memory_id: int) -> None:
        """按 `memory_id` 删除；collection 不存在或点不存在都视为成功（幂等）。"""
        try:
            if not await self._collection_exists(collection_name):
                log.info("删除记忆跳过：collection %s 不存在", collection_name)
                return
            await self._client.delete(
                collection_name=collection_name,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="memory_id",
                                match=qm.MatchValue(value=memory_id),
                            )
                        ]
                    )
                ),
                wait=True,
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
    ) -> tuple[list[MemoryVectorHit], list[str]]:
        """按用户 + 状态 + 作用域过滤后做向量检索。

        返回 `(hits, warnings)`：collection 不存在时返回空列表 + 一条 warning，
        **不在检索路径里隐式建集合**（那会掩盖写入失败）。
        """
        if top_k <= 0:
            raise MemoryVectorStoreError("记忆检索 top_k 必须为正数")
        if not query_vector:
            raise MemoryVectorStoreError("记忆检索查询向量为空")

        collection = self.collection_name(model=model, dimension=dimension)
        if not await self._collection_exists(collection):
            return [], ["memory collection 未建立"]

        must: list[qm.Condition] = [
            qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
            qm.FieldCondition(key="status", match=qm.MatchValue(value="ACTIVE")),
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

    async def _collection_exists(self, name: str) -> bool:
        try:
            return bool(await self._client.collection_exists(name))
        except Exception as exc:  # noqa: BLE001
            raise MemoryVectorStoreUnavailable(
                f"Qdrant 记忆 collection 状态查询失败：{type(exc).__name__}"
            ) from exc

    async def aclose(self) -> None:
        await self._client.close()
