"""Qdrant 文档向量存储。

关键约束：

* collection 名由「模型名 + 维度」决定，Hash 与真实模型永远不会共用一个集合；
* 每个 point 的 ID 由 `(document_id, index_version, chunk_index)` 通过 UUIDv5
  确定性生成，重复索引是覆盖而不是追加；`index_version=0` 保持迁移前的旧公式，
  这样历史 point 不会被“改名”成孤儿；
* **写入新版本前不删除旧版本**：新版本完成写入并通过精确计数校验后才由后端
  切换活跃版本，切换成功后再单独清理旧版本。索引失败时旧版本原样服务；
* payload 额外记录 `index_version` / `doc_version` / `embedding_fingerprint`，
  检索按活跃 `doc_version` 过滤，对迁移前没有这些字段的旧 point 退回
  `document_id` 白名单；
* user_id / space_id / document_id / chunk_id / doc_version / index_version
  建立 payload 索引，检索阶段先按用户、Space、活跃版本过滤再做向量搜索。
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
_PAYLOAD_INDEX_FIELDS: dict[str, qm.PayloadSchemaType] = {
    "user_id": qm.PayloadSchemaType.INTEGER,
    "space_id": qm.PayloadSchemaType.INTEGER,
    "document_id": qm.PayloadSchemaType.INTEGER,
    "chunk_id": qm.PayloadSchemaType.INTEGER,
    "index_version": qm.PayloadSchemaType.INTEGER,
    "doc_version": qm.PayloadSchemaType.KEYWORD,
}


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


def point_id_for(document_id: int, chunk_index: int, index_version: int = 0) -> str:
    """确定性 point ID。

    * `index_version <= 0`：保留迁移前公式，历史 point 仍可按原 ID 命中；
    * `index_version >= 1`：加入版本号，新旧构建互不覆盖，可以并行存在。
    """
    if index_version <= 0:
        key = f"ails-document:{document_id}:{chunk_index}"
    else:
        key = f"ails-document:{document_id}:v{index_version}:{chunk_index}"
    return str(uuid.uuid5(_POINT_NAMESPACE, key))


def doc_version_for(document_id: int, index_version: int) -> str:
    return f"{document_id}:{index_version}"


def collection_name_for(prefix: str, model: str, dimension: int) -> str:
    safe_model = re.sub(r"[^a-zA-Z0-9_-]+", "_", model).strip("_") or "model"
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "ails_documents"
    name = f"{safe_prefix}_{safe_model}_{dimension}"
    return name[:255]


def assert_vector_config(name: str, info: Any, dimension: int) -> None:
    """校验已有 collection 的向量配置与当前 Embedding 一致。

    文档集合与记忆集合共用这套规则：维度或距离函数不一致时绝不写入，
    否则会把两个不可比较的向量空间混在一起。
    """
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


async def ensure_payload_indexes(
    client: AsyncQdrantClient,
    name: str,
    info: Any,
    fields: dict[str, qm.PayloadSchemaType],
) -> None:
    """只补建缺失的 payload 索引，已有索引不重复创建。"""
    existing = set((info.payload_schema or {}).keys())
    for field_name, schema in fields.items():
        if field_name in existing:
            continue
        await client.create_payload_index(
            collection_name=name,
            field_name=field_name,
            field_schema=schema,
            wait=True,
        )


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
        assert_vector_config(name, info, dimension)

    async def _ensure_payload_indexes(self, name: str, info: Any) -> None:
        await ensure_payload_indexes(
            self._client, name, info, _PAYLOAD_INDEX_FIELDS
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
        index_version: int = 0,
        embedding_fingerprint: str | None = None,
    ) -> dict[int, str]:
        """写入一个索引版本，并在返回前校验该版本的 point 数量。

        <p>写入顺序刻意是“只写新版本、不碰旧版本”：旧版本在后端切换前继续服务。
        同一版本号的残留 point 会先清掉（正常为空），避免部分重试产生多余序号。
        写入后用精确 count 校验，部分写入不可能被报告为成功。</p>
        """
        if not chunks:
            raise DocumentVectorStoreError("没有可写入的 chunk")
        if index_version < 0:
            raise DocumentVectorStoreError("index_version 不能为负数")

        try:
            # 同一版本的重试：清掉该版本残留，保证下面的精确计数有意义。
            await self._delete_by_versions(
                collection_name=collection_name,
                document_id=document_id,
                versions=[index_version],
            )

            mapping: dict[int, str] = {}
            for start in range(0, len(chunks), self._batch_size):
                batch = chunks[start : start + self._batch_size]
                points = [
                    qm.PointStruct(
                        id=point_id_for(document_id, chunk.chunk_index, index_version),
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
                            "index_version": index_version,
                            "doc_version": doc_version_for(document_id, index_version),
                            "embedding_fingerprint": embedding_fingerprint,
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
                    mapping[chunk.chunk_id] = point_id_for(
                        document_id, chunk.chunk_index, index_version
                    )

            written = await self.count_document_points(
                collection_name=collection_name,
                document_id=document_id,
                index_version=index_version,
            )
            if written != len(chunks):
                raise DocumentVectorStoreError(
                    "Qdrant 写入不完整：期望 "
                    f"{len(chunks)} 个 point，实际 {written} 个"
                )
            return mapping
        except DocumentVectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreError(f"Qdrant 写入失败：{exc}") from exc

    async def delete_document(self, *, collection_name: str, document_id: int) -> None:
        """删除该文档的全部版本。collection 不存在时视为已删除（幂等）。"""
        try:
            if not await self._collection_exists(collection_name):
                log.info("删除文档跳过：collection %s 不存在", collection_name)
                return
            await self._client.delete(
                collection_name=collection_name,
                points_selector=qm.FilterSelector(filter=self._document_filter(document_id)),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreError(f"Qdrant 删除文档向量失败：{exc}") from exc

    async def delete_versions_except(
        self, *, collection_name: str, document_id: int, keep_index_version: int
    ) -> None:
        """删除文档中除 `keep_index_version` 外的所有版本。

        `keep_index_version == 0` 表示保留迁移前的 legacy points（没有
        `index_version` 字段），只删除新格式的其他版本；`keep > 0` 时 legacy
        points 也必须删除，否则它们会和新的活跃版本一起被检索。
        """
        try:
            if not await self._collection_exists(collection_name):
                return
            must_not: list[qm.Condition] = [
                qm.FieldCondition(
                    key="index_version",
                    match=qm.MatchValue(value=keep_index_version),
                )
            ]
            if keep_index_version <= 0:
                # must_not 中再加一条“字段非空”的否定：没有该字段的 legacy point
                # 会被 is_empty 命中，从而不进入删除集合。
                must_not.append(
                    qm.IsEmptyCondition(is_empty=qm.PayloadField(key="index_version"))
                )
            await self._client.delete(
                collection_name=collection_name,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[self._document_condition(document_id)],
                        must_not=must_not,
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreError(f"Qdrant 清理旧版本失败：{exc}") from exc

    async def delete_versions(
        self,
        *,
        collection_name: str,
        document_id: int,
        versions: list[int],
        delete_legacy: bool = False,
    ) -> None:
        """只删除指定版本；可选删除没有 `index_version` 字段的 legacy points。

        与 `delete_versions_except` 不同，这个接口不会删除未列出的版本。调用方
        先用 fencing/版本白名单确定删除集合，即使删除期间出现更高版本，也不会误删。
        """
        normalized = sorted({version for version in versions if version >= 0})
        if not normalized and not delete_legacy:
            return
        try:
            if not await self._collection_exists(collection_name):
                return
            should: list[qm.Condition] = []
            if normalized:
                should.append(
                    qm.FieldCondition(
                        key="index_version",
                        match=qm.MatchAny(any=normalized),
                    )
                )
            if delete_legacy:
                should.append(
                    qm.IsEmptyCondition(is_empty=qm.PayloadField(key="index_version"))
                )
            await self._client.delete(
                collection_name=collection_name,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[self._document_condition(document_id)],
                        should=should,
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreError(f"Qdrant 精确版本清理失败：{exc}") from exc

    async def count_document_points(
        self,
        *,
        collection_name: str,
        document_id: int,
        index_version: int | None = None,
    ) -> int:
        conditions: list[qm.Condition] = [self._document_condition(document_id)]
        if index_version is not None:
            conditions.append(
                qm.FieldCondition(
                    key="index_version",
                    match=qm.MatchValue(value=index_version),
                )
            )
        try:
            result = await self._client.count(
                collection_name=collection_name,
                count_filter=qm.Filter(must=conditions),
                exact=True,
            )
        except Exception as exc:  # noqa: BLE001 —— 对账必须区分“不可达”与“数量不一致”
            raise DocumentVectorStoreUnavailable(
                f"Qdrant point 计数失败：{type(exc).__name__}"
            ) from exc
        return int(result.count)

    async def collection_exists(self, name: str) -> bool:
        """公开的 collection 存在性查询；网络异常转换为显式不可用错误。"""
        return await self._collection_exists(name)

    async def search_knowledge(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        user_id: int,
        space_id: int,
        document_versions: list[tuple[int, int]],
        top_k: int,
        embedding_fingerprint: str | None = None,
    ) -> list[KnowledgeSearchHit]:
        """按权限 + 活跃版本过滤后做向量检索。

        权限条件缺一不可：`user_id`、`space_id`、以及
        `doc_version IN 活跃白名单`（迁移前的 version=0 文档退回 `document_id IN`）。
        空白名单直接返回空，绝不发起一次没有文档过滤的查询。
        `min_score` 不在这里应用：上层需要知道「Qdrant 有没有返回候选」，
        才能区分「无结果」与「全部低于阈值」。
        """
        if top_k <= 0:
            raise DocumentVectorStoreError("知识检索 top_k 必须为正数")
        if not document_versions:
            return []
        if not query_vector:
            raise DocumentVectorStoreError("知识检索查询向量为空")

        versioned = sorted(
            {
                doc_version_for(int(document_id), int(index_version))
                for document_id, index_version in document_versions
                if int(index_version) > 0
            }
        )
        legacy_ids = sorted(
            {
                int(document_id)
                for document_id, index_version in document_versions
                if int(index_version) <= 0
            }
        )

        must: list[qm.Condition] = [
            qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
            qm.FieldCondition(key="space_id", match=qm.MatchValue(value=space_id)),
        ]
        should: list[qm.Condition] = []
        if versioned:
            should.append(
                qm.FieldCondition(key="doc_version", match=qm.MatchAny(any=versioned))
            )
        if legacy_ids:
            should.append(
                qm.FieldCondition(key="document_id", match=qm.MatchAny(any=legacy_ids))
            )
        if not should:
            return []

        query_filter = qm.Filter(must=must, should=should)
        if embedding_fingerprint:
            # 纵深防御：不同网关的相同模型名可能落进同一 collection。
            # legacy point 没有该字段，不能被 must_not 排除，所以这里不把它作为
            # Qdrant 过滤条件，而是在 payload 校验阶段拒绝不匹配的候选。
            pass

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

    # ---------- 内部 ----------

    async def _collection_exists(self, name: str) -> bool:
        try:
            return bool(await self._client.collection_exists(name))
        except Exception as exc:  # noqa: BLE001
            raise DocumentVectorStoreUnavailable(
                f"Qdrant collection 状态查询失败：{exc}"
            ) from exc

    async def _delete_by_versions(
        self, *, collection_name: str, document_id: int, versions: list[int]
    ) -> None:
        if not versions:
            return
        await self._client.delete(
            collection_name=collection_name,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        self._document_condition(document_id),
                        qm.FieldCondition(
                            key="index_version",
                            match=qm.MatchAny(any=sorted(set(versions))),
                        ),
                    ]
                )
            ),
            wait=True,
        )

    @staticmethod
    def _document_condition(document_id: int) -> qm.FieldCondition:
        return qm.FieldCondition(
            key="document_id",
            match=qm.MatchValue(value=document_id),
        )

    @classmethod
    def _document_filter(cls, document_id: int) -> qm.Filter:
        return qm.Filter(must=[cls._document_condition(document_id)])

    async def aclose(self) -> None:
        await self._client.close()
