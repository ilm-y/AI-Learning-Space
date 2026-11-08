"""真实文档知识检索：查询向量 → Qdrant 过滤检索 → KnowledgeChunk。

这层只做「已经被 Spring 授权的文档白名单」之上的向量检索：

```text
KnowledgeQuery(userId, spaceId, question)
    → Embedding Provider（与索引时同一个 model/dimension）
    → Qdrant filter: user_id AND space_id AND document_id IN allowed_ids
    → Top-K → 阈值过滤 → 校验 payload
```

它不访问 MySQL，也不自行决定权限；白名单由后端 `ready-ids` 接口给出。
任何依赖失败都返回带 warning 的空结果，**绝不**退化成无过滤搜索，
也绝不把 Hash 向量和真实模型向量混在同一个 collection 里。
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

from app.embedding.base import EmbeddingProvider
from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.schemas import KnowledgeChunk
from app.vectorstore.qdrant_store import (
    KnowledgeSearchHit,
    QdrantDocumentStore,
    collection_name_for,
    doc_version_for,
)

log = logging.getLogger(__name__)

_MAX_ALLOWED_DOCUMENTS = 10_000


@dataclass(frozen=True)
class AllowedDocument:
    """后端 ready-ids 返回的白名单项：文档 ID + 当前活跃索引版本。"""

    document_id: int
    index_version: int


class KnowledgeSearchService:
    """把现有 Embedding Provider 与 Qdrant Store 串成一次知识检索。"""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        store: QdrantDocumentStore,
        collection_prefix: str,
        timeout_seconds: float,
        require_embedding_fingerprint: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("knowledge timeout 必须为正数")
        self._provider = provider
        self._store = store
        self._prefix = collection_prefix
        self._timeout = timeout_seconds
        # RAG-04 阶段 A：默认关闭，行为与历史一致。开启后 payload 中缺失/空白/
        # 不匹配的 fingerprint 都会被丢弃，绝不把身份未知的 legacy point 当作兼容。
        self._require_fingerprint = bool(require_embedding_fingerprint)

    @property
    def embedding_model(self) -> str:
        """当前真实生效的模型名；后端白名单据此排除旧模型索引的文档。"""
        return self._provider.model

    @property
    def embedding_provider(self) -> str:
        return self._provider.name

    @property
    def embedding_dimension(self) -> int:
        return self._provider.dimension

    @property
    def embedding_fingerprint(self) -> str:
        return self._provider.fingerprint

    @property
    def require_embedding_fingerprint(self) -> bool:
        """正常检索是否要求使用当前 fingerprint（严格模式）。"""
        return self._require_fingerprint

    async def search(
        self, query: KnowledgeQuery, allowed_documents: list[AllowedDocument]
    ) -> KnowledgeResult:
        if not query.question.strip():
            return KnowledgeResult(
                status=KnowledgeStatus.NO_MATCH,
                warning="本轮问题为空，未执行文档检索。",
            )

        allowed = _normalize_allowed_documents(allowed_documents)
        if allowed is None:
            log.warning(
                "文档白名单格式异常，知识检索 fail closed：user=%s space=%s",
                query.user_id,
                query.space_id,
            )
            return KnowledgeResult.degraded("文档白名单格式异常，本次不使用文档资料。")
        if not allowed:
            log.info(
                "知识检索跳过：user=%s space=%s status=%s",
                query.user_id,
                query.space_id,
                KnowledgeStatus.NO_VALID_DOCUMENTS.value,
            )
            return KnowledgeResult(
                status=KnowledgeStatus.NO_VALID_DOCUMENTS,
                warning="本空间没有已就绪且索引完整的文档，本次回答未使用上传资料。",
            )

        # Runtime 调用契约：严格模式下正常检索必须使用当前 fingerprint。
        # provider 正常实现都会给出非空值；这里显式 fail closed，防止未来新增
        # 不带 fingerprint 的调用路径悄悄绕过严格策略。
        if self._require_fingerprint and not self._provider.fingerprint.strip():
            log.warning(
                "严格指纹模式缺少当前 fingerprint，知识检索 fail closed：user=%s space=%s",
                query.user_id,
                query.space_id,
            )
            return KnowledgeResult.degraded(
                "严格指纹模式要求当前 Embedding 配置具有非空 fingerprint，"
                "本次未使用文档资料。"
            )

        try:
            return await asyncio.wait_for(
                self._search_checked(query, allowed),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "知识检索超时：user=%s space=%s timeout=%.1fs",
                query.user_id,
                query.space_id,
                self._timeout,
            )
            return KnowledgeResult.degraded("知识检索超时，本次回答未使用文档资料。")
        except Exception as exc:  # noqa: BLE001 —— 普通检索故障必须降级，不能打挂对话
            log.warning(
                "知识检索失败：user=%s space=%s error=%s",
                query.user_id,
                query.space_id,
                type(exc).__name__,
            )
            return KnowledgeResult.degraded("知识检索依赖不可用，本次回答未使用文档资料。")

    # ---------- 内部 ----------

    async def _search_checked(
        self, query: KnowledgeQuery, allowed: list[AllowedDocument]
    ) -> KnowledgeResult:
        vectors = await self._provider.embed([query.question])
        if len(vectors) != 1:
            raise RuntimeError("Embedding Provider 未返回单条查询向量")
        query_vector = vectors[0]
        if len(query_vector) != self._provider.dimension:
            raise RuntimeError("查询向量维度与 Embedding Provider 不一致")

        collection = collection_name_for(
            self._prefix,
            self._provider.model,
            self._provider.dimension,
        )
        hits = await self._store.search_knowledge(
            collection_name=collection,
            query_vector=query_vector,
            user_id=query.user_id,
            space_id=query.space_id,
            document_versions=[
                (item.document_id, item.index_version) for item in allowed
            ],
            top_k=query.top_k,
            embedding_fingerprint=self._provider.fingerprint,
        )
        if not hits:
            log.info(
                "知识检索完成：user=%s space=%s status=%s candidates=0 collection=%s",
                query.user_id,
                query.space_id,
                KnowledgeStatus.NO_MATCH.value,
                collection,
            )
            return KnowledgeResult(
                status=KnowledgeStatus.NO_MATCH,
                warning="知识检索没有命中与本轮问题相关的文档片段。",
            )

        allowed_versions = {item.document_id: item.index_version for item in allowed}
        above_threshold = [
            hit for hit in hits if math.isfinite(hit.score) and hit.score >= query.min_score
        ]
        if not above_threshold:
            log.info(
                "知识检索完成：user=%s space=%s status=%s candidates=%s collection=%s",
                query.user_id,
                query.space_id,
                KnowledgeStatus.THRESHOLD_FILTERED.value,
                len(hits),
                collection,
            )
            return KnowledgeResult(
                status=KnowledgeStatus.THRESHOLD_FILTERED,
                warning=(
                    "检索到文档候选，但相似度均低于阈值 "
                    f"{query.min_score:.2f}，本次回答未使用文档资料。"
                ),
                candidates=len(hits),
            )

        chunks: list[KnowledgeChunk] = []
        invalid = 0
        seen: set[int] = set()
        for hit in above_threshold:
            chunk = _parse_hit(
                hit,
                allowed_versions,
                self._provider.fingerprint,
                require_fingerprint=self._require_fingerprint,
            )
            if chunk is None:
                invalid += 1
                continue
            if chunk.id in seen:
                continue
            seen.add(chunk.id)
            chunks.append(chunk)

        log.info(
            "知识检索完成：user=%s space=%s status=%s candidates=%s aboveThreshold=%s "
            "valid=%s invalid=%s collection=%s",
            query.user_id,
            query.space_id,
            KnowledgeStatus.SUCCESS.value,
            len(hits),
            len(above_threshold),
            len(chunks),
            invalid,
            collection,
        )

        if not chunks:
            return KnowledgeResult(
                status=KnowledgeStatus.DEGRADED,
                warning="检索结果的必要字段不完整，本次回答未使用文档资料。",
                candidates=len(hits),
            )

        warnings: list[str] = []
        if invalid:
            warnings.append(f"有 {invalid} 条检索结果字段不完整，已忽略。")
        return KnowledgeResult(
            chunks=chunks,
            status=KnowledgeStatus.SUCCESS,
            warning=" ".join(warnings) or None,
            candidates=len(hits),
        )


def _normalize_allowed_documents(
    values: list[AllowedDocument],
) -> list[AllowedDocument] | None:
    """白名单必须整体合法；任何一条异常都按 fail closed 处理，不部分接受。"""
    if not isinstance(values, list):
        return None
    if len(values) > _MAX_ALLOWED_DOCUMENTS:
        return None
    normalized: dict[int, int] = {}
    for value in values:
        if not isinstance(value, AllowedDocument):
            return None
        if (
            isinstance(value.document_id, bool)
            or not isinstance(value.document_id, int)
            or value.document_id <= 0
        ):
            return None
        if (
            isinstance(value.index_version, bool)
            or not isinstance(value.index_version, int)
            or value.index_version < 0
        ):
            return None
        # 同一文档出现两个版本说明后端状态不一致，整体拒绝，绝不挑一个继续。
        if value.document_id in normalized and normalized[value.document_id] != value.index_version:
            return None
        normalized[value.document_id] = value.index_version
    return [
        AllowedDocument(document_id=document_id, index_version=index_version)
        for document_id, index_version in sorted(normalized.items())
    ]


def _parse_hit(
    hit: KnowledgeSearchHit,
    allowed_versions: dict[int, int],
    expected_fingerprint: str,
    *,
    require_fingerprint: bool = False,
) -> KnowledgeChunk | None:
    payload = hit.payload
    if not isinstance(payload, dict):
        return None

    chunk_id = _positive_int(payload.get("chunk_id"))
    document_id = _positive_int(payload.get("document_id"))
    chunk_index = _non_negative_int(payload.get("chunk_index"))
    content = payload.get("content")
    if chunk_id is None or document_id is None or chunk_index is None:
        return None
    if document_id not in allowed_versions:
        # 纵深防御：即使 Qdrant filter 被绕过，也不接受白名单外文档。
        return None

    # 版本隔离：只接受后端白名单中该文档的活跃 doc_version。
    expected_version = allowed_versions[document_id]
    payload_version = payload.get("index_version")
    if expected_version > 0:
        if payload.get("doc_version") != doc_version_for(document_id, expected_version):
            return None
    else:
        # 迁移前的 legacy point 没有 index_version；有值且非 0 的不能混进来。
        if payload_version is not None and payload_version != 0:
            return None

    # Embedding 兼容性：同一模型名可能被不同网关复用，collection 名无法区分。
    payload_fingerprint = payload.get("embedding_fingerprint")
    if require_fingerprint:
        # 严格模式：payload 必须带非空 fingerprint 且与当前配置精确一致。
        # 缺失（legacy point）或空白一律丢弃，不做降级兼容。
        if not isinstance(payload_fingerprint, str) or not payload_fingerprint.strip():
            return None
        if payload_fingerprint.strip() != expected_fingerprint:
            return None
    elif isinstance(payload_fingerprint, str) and payload_fingerprint.strip():
        if payload_fingerprint.strip() != expected_fingerprint:
            return None

    if not isinstance(content, str) or not content.strip():
        return None

    document_name = payload.get("file_name") or payload.get("document_name")
    if not isinstance(document_name, str) or not document_name.strip():
        return None

    file_type = payload.get("file_type")
    if file_type is not None and not isinstance(file_type, str):
        return None

    return KnowledgeChunk(
        id=chunk_id,
        document_id=document_id,
        chunk_index=chunk_index,
        document_name=document_name.strip()[:255],
        file_type=file_type.strip()[:64] if isinstance(file_type, str) else None,
        heading_path=_heading_path(payload.get("metadata")),
        content=content,
        score=float(hit.score),
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _heading_path(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("headingPath")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw[:10]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:200])
    return result
