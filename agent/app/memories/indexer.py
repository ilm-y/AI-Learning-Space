"""MemoryIndexer：把一条记忆同步到 Qdrant。

职责只有三件事：计算 content hash、嵌入、写入/删除。它不读 MySQL，
也不判断事件是否应该产生 —— 那是 Spring Outbox 的职责。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.documents.errors import DocumentEmbeddingError
from app.embedding.base import EmbeddingProvider, validate_vectors
from app.memories.errors import (
    MemoryEmbeddingError,
    MemoryEmbeddingUnavailable,
)
from app.memories.schemas import MemorySyncRequest
from app.vectorstore.memory_store import QdrantMemoryStore, point_id_for_memory

log = logging.getLogger(__name__)


def content_hash_for(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class MemoryIndexer:
    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        store: QdrantMemoryStore,
    ) -> None:
        self._provider = provider
        self._store = store

    async def sync(self, request: MemorySyncRequest) -> dict[str, Any]:
        if request.is_delete:
            return await self._delete(request)
        return await self._upsert(request)

    async def _delete(self, request: MemorySyncRequest) -> dict[str, Any]:
        collection = self._store.collection_name(
            model=self._provider.model,
            dimension=self._provider.dimension,
        )
        await self._store.delete_memory(
            collection_name=collection, memory_id=request.memory_id
        )
        log.info(
            "Memory vector deleted memoryId=%s userId=%s eventId=%s",
            request.memory_id,
            request.user_id,
            request.event_id,
        )
        return {"pointId": None, "collection": collection, "skipped": False}

    async def _upsert(self, request: MemorySyncRequest) -> dict[str, Any]:
        # UPSERT 才建集合：DELETE 不应该因为清理一条旧数据而创建空 collection。
        collection = await self._store.ensure_collection(
            model=self._provider.model,
            dimension=self._provider.dimension,
        )
        point_id = point_id_for_memory(request.memory_id)
        content = request.content or ""
        content_hash = (request.content_hash or "").strip() or content_hash_for(content)

        payload = self._build_payload(
            request=request,
            content_hash=content_hash,
        )

        existing = await self._store.retrieve_payload(
            collection_name=collection, point_id=point_id
        )
        if self._unchanged(existing, payload):
            log.info(
                "Memory vector unchanged, skipped memoryId=%s eventId=%s",
                request.memory_id,
                request.event_id,
            )
            return {"pointId": point_id, "collection": collection, "skipped": True}

        vector = await self._embed(content)
        await self._store.upsert_memory(
            collection_name=collection,
            point_id=point_id,
            vector=vector,
            payload=payload,
        )
        log.info(
            "Memory vector upserted memoryId=%s userId=%s version=%s eventId=%s",
            request.memory_id,
            request.user_id,
            request.memory_version,
            request.event_id,
        )
        return {"pointId": point_id, "collection": collection, "skipped": False}

    async def _embed(self, content: str) -> list[float]:
        try:
            vectors = await self._provider.embed([content])
            validated = validate_vectors(
                vectors,
                expected_count=1,
                dimension=self._provider.dimension,
                provider=self._provider.name,
            )
        except DocumentEmbeddingError as exc:
            error_type = (
                MemoryEmbeddingUnavailable
                if getattr(exc, "status_code", 502) == 503
                else MemoryEmbeddingError
            )
            raise error_type(str(exc)) from exc
        return validated[0]

    def _build_payload(
        self,
        *,
        request: MemorySyncRequest,
        content_hash: str,
    ) -> dict[str, Any]:
        # 正文不进入 payload：检索只返回 id 与分数，正文由 Spring 回 MySQL 取。
        return {
            "memory_id": request.memory_id,
            "user_id": request.user_id,
            "status": request.status,
            "scope_type": request.scope_type,
            "space_id": request.learning_space_id,
            "concept_id": request.concept_id,
            "memory_type": request.memory_type,
            "importance": request.importance,
            "content_hash": content_hash,
            "memory_version": request.memory_version,
            "embedding_fingerprint": self._provider.fingerprint,
        }

    @staticmethod
    def _unchanged(
        existing: dict[str, Any] | None, desired: dict[str, Any]
    ) -> bool:
        """内容与全部过滤相关字段一致时才跳过重嵌入。

        只比较 content_hash 是不够的：记忆可能从 ACTIVE 变为 ARCHIVED，
        或作用域变化。此时向量可以不变，但 payload 必须更新，否则检索会
        继续按旧状态/旧作用域召回。
        """
        if not existing:
            return False
        return all(existing.get(key) == value for key, value in desired.items())
