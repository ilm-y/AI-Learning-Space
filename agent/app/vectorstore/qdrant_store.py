"""Qdrant 文档向量存储。

关键约束：

* collection 名由「模型名 + 维度」决定，Hash 与真实模型永远不会共用一个集合；
* 每个 point 的 ID 由 `(document_id, chunk_index)` 通过 UUIDv5 确定性生成，
  重复索引是覆盖而不是追加；
* 写入前只按 `document_id` 删除该文档的旧 points，绝不重建整个 collection；
* user_id / space_id / document_id / chunk_id 建立 payload 索引，检索阶段可以
  先按用户与 Space 过滤，再做向量搜索。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from app.documents.errors import DocumentVectorStoreError, DocumentVectorStoreUnavailable

log = logging.getLogger(__name__)

_POINT_NAMESPACE = uuid.UUID("b7f3a5d0-2c1e-4a6f-9f8e-1d2c3b4a5e6f")
_PAYLOAD_INDEX_FIELDS = ("user_id", "space_id", "document_id", "chunk_id")


@dataclass(frozen=True)
class ChunkVector:
    chunk_id: int
    chunk_index: int
    content: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeSearchHit:
    """Qdrant 返回的一条候选；payload 由上层做严格校验。"""

    score: float
    payload: dict[str, Any]


def point_id_for(document_id: int, chunk_index: int) -> str:
    """确定性 point ID：同一文档同一序号始终映射到同一个 UUID。"""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"ails-document:{document_id}:{chunk_index}"))


def collection_name_for(prefix: str, model: str, dimension: int) -> str:
    safe_model = re.sub(r"[^a-zA-Z0-9_-]+", "_", model).strip("_") or "model"
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "ails_documents"
    name = f"{safe_prefix}_{safe_model}_{dimension}"
    return name[:255]


class QdrantDocumentStore:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: float,
        collection_prefix: str,
        upsert_batch_size: int,
    ) -> None:
        if not url.strip():
            raise ValueError("Qdrant URL 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("Qdrant timeout 必须为正数")
        if upsert_batch_size <= 0:
            raise ValueError("Qdrant upsert 批次大小必须为正数")

        self._prefix = collection_prefix
        self._batch_size = upsert_batch_size
        self._client = AsyncQdrantClient(
            url=url,
            api_key=api_key or None,
            timeout=max(1, int(timeout_seconds)),
            prefer_grpc=False,
            check_compatibility=False,
        )
        self._ensure_lock = asyncio.Lock()

    # ===================== collection =====================

    async def ensure_collection(self, *, model: str, dimension: int) -> str:
        name = collection_name_for(self._prefix, model, dimension)
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
                    log.info("Created Qdrant collection=%s, dimension=%s", name, dimension)
                info = await self._client.get_collection(name)
                self._assert_vector_config(name, info, dimension)
                await self._ensure_payload_indexes(name, info)
            except DocumentVectorStoreError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 连接层异常统一转成显式存储错误
                raise DocumentVectorStoreUnavailable(
                    f"Qdrant 不可用或集合初始化失败：{exc}"
                ) from exc
        return name

    def _assert_vector_config(self, name: str, info: Any, dimension: int) -> None:
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):  # named vectors：本阶段只支持单个默认向量
            if len(vectors) != 1:
                raise DocumentVectorStoreError(
                    f"Qdrant collection {name} 包含多个命名向量，无法安全写入"
                )
            vectors = next(iter(vectors.values()))
        size = getattr(vectors, "size", None)
        if size != dimension:
            raise DocumentVectorStoreError(
                f"Qdrant collection {name} 维度不一致：期望 {dimension}，实际 {size}"
            )
        distance = getattr(vectors, "distance", None)
        if distance is not None and str(distance).lower() not in {
            "distance.cosine",
            "cosine",
        }:
            raise DocumentVectorStoreError(
                f"Qdrant collection {name} 距离函数不是 COSINE：{distance}"
            )

    async def _ensure_payload_indexes(self, name: str, info: Any) -> None:
        existing = set((info.payload_schema or {}).keys())
        for field_name in _PAYLOAD_INDEX_FIELDS:
            if field_name in existing:
                continue
            await self._client.create_payload_index(
                collection_name=name,
                field_name=field_name,
                field_schema=qm.PayloadSchemaType.INTEGER,
                wait=True,
            )

    # ===================== 写入 / 删除 =====================

    async def index_document(
        self,
        *,
        collection_name: str,
        document_id: int,
        user_id: int,
        space_id: int,
        document_name: str,
        file_type: str,
        chunks: list[ChunkVector],
        file_name: str | None = None,
    ) -> dict[int, str]:
        if not chunks:
            raise DocumentVectorStoreError("没有可写入的 chunk")

        try:
            # 先删除该文档的旧 points，再写入新版本；全程只影响 document_id 范围。
            await self._client.delete(
                collection_name=collection_name,
                points_selector=qm.FilterSelector(filter=self._document_filter(document_id)),
                wait=True,
            )
            mapping: dict[int, str] = {}
            for start in range(0, len(chunks), self._batch_size):
                batch = chunks[start : start + self._batch_size]
                points = [
                    qm.PointStruct(
                        id=point_id_for(document_id, chunk.chunk_index),
                        vector=chunk.vector,
                        payload={
                            "user_id": user_id,
                            "space_id": space_id,
                            "document_id": document_id,
                            "chunk_id": chunk.chunk_id,
                            "chunk_index": chunk.chunk_index,
                            "document_name": document_name,
                            "file_name": file_name or document_name,
                            "file_type": file_type,
                            "content": chunk.content,
                            "metadata": chunk.metadata,
                        },
                    )
                    for chunk in batch
                ]
                await self._client.upsert(
                    collection_name=collection_name,
                    points=points,
                    wait=True,
                )
                for chunk in batch:
                    mapping[chunk.chunk_id] = point_id_for(document_id, chunk.chunk_index)
            return mapping
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreError(f"Qdrant 写入失败：{exc}") from exc

    async def delete_document(self, *, collection_name: str, document_id: int) -> None:
        try:
            await self._client.delete(
                collection_name=collection_name,
                points_selector=qm.FilterSelector(filter=self._document_filter(document_id)),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreError(f"Qdrant 删除文档向量失败：{exc}") from exc

    async def count_document_points(self, *, collection_name: str, document_id: int) -> int:
        result = await self._client.count(
            collection_name=collection_name,
            count_filter=self._document_filter(document_id),
            exact=True,
        )
        return int(result.count)

    async def search_knowledge(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        user_id: int,
        space_id: int,
        document_ids: list[int],
        top_k: int,
    ) -> list[KnowledgeSearchHit]:
        """按权限过滤后做向量检索。

        三个过滤条件缺一不可：`user_id`、`space_id`、`document_id IN 白名单`。
        空白名单直接返回空，绝不发起一次没有 document_id 过滤的查询。
        `min_score` 不在这里应用：上层需要知道「Qdrant 有没有返回候选」，
        才能区分「无结果」与「全部低于阈值」。
        """
        if top_k <= 0:
            raise DocumentVectorStoreError("知识检索 top_k 必须为正数")
        if not document_ids:
            return []
        if not query_vector:
            raise DocumentVectorStoreError("知识检索查询向量为空")

        unique_ids = sorted({int(value) for value in document_ids})
        query_filter = qm.Filter(
            must=[
                qm.FieldCondition(
                    key="user_id",
                    match=qm.MatchValue(value=user_id),
                ),
                qm.FieldCondition(
                    key="space_id",
                    match=qm.MatchValue(value=space_id),
                ),
                qm.FieldCondition(
                    key="document_id",
                    match=qm.MatchAny(any=unique_ids),
                ),
            ]
        )

        try:
            result = await self._client.query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
            hits: list[KnowledgeSearchHit] = []
            for point in result.points:
                score = getattr(point, "score", None)
                payload = getattr(point, "payload", None)
                if not isinstance(score, (int, float)):
                    continue
                hits.append(
                    KnowledgeSearchHit(score=float(score), payload=payload or {})
                )
            return hits
        except DocumentVectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一转成显式依赖错误，上层降级
            raise DocumentVectorStoreUnavailable(
                f"Qdrant 知识检索失败：{type(exc).__name__}"
            ) from exc

    @staticmethod
    def _document_filter(document_id: int) -> qm.Filter:
        return qm.Filter(
            must=[
                qm.FieldCondition(
                    key="document_id",
                    match=qm.MatchValue(value=document_id),
                )
            ]
        )

    async def aclose(self) -> None:
        await self._client.close()
