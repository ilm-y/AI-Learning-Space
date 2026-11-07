"""MemoryVectorSearch：query → Embedding → Qdrant。

只返回 `memoryId / score / contentHash`，**绝不返回正文**。Spring 必须用
MySQL 回源复核 ACTIVE 状态与可见性，因此这里的任何陈旧向量都不会直接
变成用户可见内容。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.documents.errors import DocumentEmbeddingError
from app.embedding.base import EmbeddingProvider, validate_vectors
from app.memories.errors import (
    MemoryEmbeddingError,
    MemoryEmbeddingUnavailable,
    MemoryVectorStoreUnavailable,
)
from app.memories.schemas import MemorySearchRequest
from app.vectorstore.memory_store import QdrantMemoryStore

log = logging.getLogger(__name__)


class MemoryVectorSearch:
    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        store: QdrantMemoryStore,
        min_score: float = 0.0,
        timeout_seconds: float = 2.0,
        default_top_k: int = 20,
    ) -> None:
        self._provider = provider
        self._store = store
        self._min_score = min_score
        self._timeout_seconds = timeout_seconds
        self._default_top_k = default_top_k

    async def search(self, request: MemorySearchRequest) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._search(request), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise MemoryVectorStoreUnavailable(
                f"记忆向量检索超时（{self._timeout_seconds}s）"
            ) from exc

    async def _search(self, request: MemorySearchRequest) -> dict[str, Any]:
        vector = await self._embed(request.query)
        collection = self._store.collection_name(
            model=self._provider.model,
            dimension=self._provider.dimension,
        )
        hits, warnings = await self._store.search(
            query_vector=vector,
            user_id=request.user_id,
            space_id=request.space_id,
            concept_id=request.concept_id,
            top_k=request.recall_k or self._default_top_k,
            model=self._provider.model,
            dimension=self._provider.dimension,
        )

        # 不同网关的相同模型名可能落进同一个 collection；指纹不同的候选
        # 属于另一个向量空间，不能参与本次排名。
        filtered = []
        mismatch = 0
        for hit in hits:
            if hit.embedding_fingerprint != self._provider.fingerprint:
                mismatch += 1
                continue
            if self._min_score > 0 and hit.score < self._min_score:
                continue
            filtered.append(hit)
        if mismatch:
            warnings = [*warnings, "已忽略 embedding 指纹不匹配的记忆向量"]
            log.warning(
                "Memory vector fingerprint mismatch dropped=%s requestId=%s",
                mismatch,
                request.request_id,
            )

        return {
            "collection": collection,
            "warnings": warnings,
            "hits": [
                {
                    "memoryId": hit.memory_id,
                    "score": hit.score,
                    "contentHash": hit.content_hash,
                }
                for hit in filtered
            ],
        }

    async def _embed(self, query: str) -> list[float]:
        try:
            vectors = await self._provider.embed([query])
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
