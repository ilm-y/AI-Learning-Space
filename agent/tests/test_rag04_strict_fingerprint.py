"""RAG-04 阶段 A：严格指纹模式 9 场景回归矩阵（Agent 侧）。

对应任务 T5 的 9 个场景。后端 SQL 白名单/索引状态机由 Java 测试覆盖，
这里验证 Agent 在「白名单 + Qdrant payload + Runtime 调用契约」三层的真实行为。

所有测试离线运行：不连接真实 Qdrant、Backend 或 Embedding API。
"""

from __future__ import annotations

import httpx
import pytest

from app.embedding import HashEmbeddingProvider
from app.retriever.base import KnowledgeQuery, KnowledgeStatus
from app.retriever.http_backend import HttpBackendRetriever
from app.retriever.knowledge import AllowedDocument, KnowledgeSearchService
from app.vectorstore.qdrant_store import (
    KnowledgeSearchHit,
    collection_name_for,
    doc_version_for,
)

CURRENT = "v1:current-endpoint"
OTHER = "v1:other-endpoint"
MODEL = "text-embedding-v4"
MISSING = object()


class FixedFingerprintProvider(HashEmbeddingProvider):
    """模型/维度相同、只有 endpoint 身份不同的 Provider。"""

    name = "openai-compatible"
    model = MODEL

    def __init__(self, fingerprint: str = CURRENT, dimension: int = 8) -> None:
        super().__init__(dimension=dimension)
        self._fingerprint = fingerprint

    @property
    def fingerprint(self) -> str:  # type: ignore[override]
        return self._fingerprint


class FakeStore:
    def __init__(self, hits: list[KnowledgeSearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict] = []

    async def search_knowledge(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


def hit(
    *,
    chunk_id: int = 11,
    document_id: int = 101,
    chunk_index: int = 0,
    score: float = 0.9,
    index_version: int | None = None,
    doc_version: str | None = None,
    embedding_fingerprint: object = MISSING,
    content: str = "TLB 是页表项缓存。",
) -> KnowledgeSearchHit:
    payload = {
        "user_id": 7,
        "space_id": 9,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "file_name": "操作系统笔记.md",
        "file_type": "md",
        "content": content,
        "metadata": {"headingPath": ["虚拟内存", "TLB"]},
    }
    if index_version is not None:
        payload["index_version"] = index_version
    if doc_version is not None:
        payload["doc_version"] = doc_version
    if embedding_fingerprint is not MISSING:
        payload["embedding_fingerprint"] = embedding_fingerprint
    return KnowledgeSearchHit(score=score, payload=payload)


def service(
    store: FakeStore,
    *,
    fingerprint: str = CURRENT,
    require: bool = True,
    provider: HashEmbeddingProvider | None = None,
) -> KnowledgeSearchService:
    return KnowledgeSearchService(
        provider=provider or FixedFingerprintProvider(fingerprint),
        store=store,
        collection_prefix="test_docs",
        timeout_seconds=5,
        require_embedding_fingerprint=require,
    )


def query() -> KnowledgeQuery:
    return KnowledgeQuery(
        user_id=7,
        space_id=9,
        question="TLB 是什么？",
        top_k=5,
        min_score=0.35,
    )


# ===================== 场景 1-5：指纹身份 =====================


async def test_scenario1_matching_fingerprint_is_whitelisted() -> None:
    store = FakeStore(
        [
            hit(
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            )
        ]
    )

    result = await service(store).search(query(), [AllowedDocument(101, 2)])

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [11]
    # Runtime 契约：正常检索把当前 fingerprint 一并传给 Qdrant filter。
    assert store.calls[0]["embedding_fingerprint"] == CURRENT
    assert store.calls[0]["collection_name"] == collection_name_for(
        "test_docs", MODEL, 8
    )


async def test_scenario2_null_fingerprint_is_excluded_in_strict_mode() -> None:
    store = FakeStore([hit(index_version=2, doc_version=doc_version_for(101, 2))])

    result = await service(store).search(query(), [AllowedDocument(101, 2)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED
    assert result.candidates == 1


async def test_scenario3_mismatched_fingerprint_is_excluded_in_strict_mode() -> None:
    store = FakeStore(
        [
            hit(
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=OTHER,
            )
        ]
    )

    result = await service(store).search(query(), [AllowedDocument(101, 2)])

    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_scenario4_blank_fingerprint_is_rejected_without_downgrade(
    blank: str,
) -> None:
    store = FakeStore(
        [
            hit(
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=blank,
            )
        ]
    )

    result = await service(store).search(query(), [AllowedDocument(101, 2)])

    # 不能因为 payload 缺 fingerprint 就退回“仅模型匹配”的兼容分支。
    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED
    assert "字段不完整" in (result.warning or "")


async def test_scenario4_runtime_contract_requires_current_fingerprint() -> None:
    store = FakeStore([hit(index_version=2, doc_version=doc_version_for(101, 2))])

    result = await service(store, fingerprint="   ").search(
        query(), [AllowedDocument(101, 2)]
    )

    # 当前配置没有 fingerprint 时，严格模式在查询前就 fail closed。
    assert result.status is KnowledgeStatus.DEGRADED
    assert store.calls == []


async def test_scenario5_same_model_different_endpoint_never_mixes_results() -> None:
    # 两个 point 的 model/dimension/collection 完全相同，只有 endpoint 身份不同：
    # 这正是 “场景 2：同模型同维度、不同 endpoint” 在 Agent 侧的可见形态。
    store = FakeStore(
        [
            hit(
                chunk_id=11,
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            ),
            hit(
                chunk_id=12,
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=OTHER,
            ),
        ]
    )

    result = await service(store).search(query(), [AllowedDocument(101, 2)])

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [11]
    assert result.candidates == 2
    assert "1 条检索结果字段不完整" in (result.warning or "")


# ===================== 场景 6：诊断结果不被 Agent 消费 =====================


def http_retriever(search: KnowledgeSearchService) -> tuple[HttpBackendRetriever, list[str]]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"code": 0, "data": [{"id": 101, "indexVersion": 2}]},
        )

    retriever = HttpBackendRetriever(
        "http://backend.test",
        auth_token="user-jwt",
        knowledge_search=search,
    )
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever, paths


async def test_scenario6_agent_only_calls_ready_ids_never_legacy_diagnostics() -> None:
    store = FakeStore(
        [
            hit(
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            )
        ]
    )
    retriever, paths = http_retriever(service(store))
    try:
        result = await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert result.status is KnowledgeStatus.SUCCESS
    assert paths == ["/api/spaces/9/documents/ready-ids"]
    # 诊断端点从未出现在正常检索的请求路径中。
    assert all("/diagnostics/" not in path for path in paths)


async def test_scenario6_strict_runtime_fails_closed_without_any_diagnostics_fallback() -> None:
    store = FakeStore()
    retriever, paths = http_retriever(service(store, fingerprint=""))
    try:
        result = await retriever.get_knowledge(query())
    finally:
        await retriever.aclose()

    assert result.status is KnowledgeStatus.DEGRADED
    # 没有 fingerprint 时连 ready-ids 都不请求，更不会退到诊断接口。
    assert paths == []
    assert store.calls == []


# ===================== 场景 7-9：重建窗口 =====================


async def test_scenario7_rebuild_in_progress_only_old_version_serves() -> None:
    store = FakeStore(
        [
            hit(
                chunk_id=11,
                index_version=1,
                doc_version=doc_version_for(101, 1),
                embedding_fingerprint=CURRENT,
            ),
            hit(
                chunk_id=12,
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            ),
        ]
    )

    # 重建期间白名单仍指向活跃版本 1；staging 版本 2 不参与检索。
    result = await service(store).search(query(), [AllowedDocument(101, 1)])

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [11]


async def test_scenario8_rebuild_success_new_version_serves_old_version_not_hit() -> None:
    store = FakeStore(
        [
            hit(
                chunk_id=11,
                index_version=1,
                doc_version=doc_version_for(101, 1),
                embedding_fingerprint=CURRENT,
            ),
            hit(
                chunk_id=12,
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            ),
        ]
    )

    # 切换完成后白名单指向版本 2；旧版本 point 即使还在 Qdrant 中也不被接受。
    result = await service(store).search(query(), [AllowedDocument(101, 2)])

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [12]


async def test_scenario9_rebuild_failure_keeps_old_version_searchable() -> None:
    store = FakeStore(
        [
            hit(
                chunk_id=12,
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            )
        ]
    )
    # 失败后后端仍返回旧活跃版本 1。
    failed = await service(store).search(query(), [AllowedDocument(101, 1)])
    assert failed.chunks == []
    assert failed.status is KnowledgeStatus.DEGRADED

    store.hits = [
        hit(
            chunk_id=11,
            index_version=1,
            doc_version=doc_version_for(101, 1),
            embedding_fingerprint=CURRENT,
        )
    ]
    survived = await service(store).search(query(), [AllowedDocument(101, 1)])
    assert survived.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in survived.chunks] == [11]


# ===================== 开关关闭时行为与历史一致 =====================


def test_switch_defaults_to_false() -> None:
    search = KnowledgeSearchService(
        provider=FixedFingerprintProvider(),
        store=FakeStore(),
        collection_prefix="test_docs",
        timeout_seconds=5,
    )

    assert search.require_embedding_fingerprint is False


async def test_switch_false_accepts_legacy_point_without_fingerprint() -> None:
    store = FakeStore([hit(index_version=2, doc_version=doc_version_for(101, 2))])

    result = await service(store, require=False).search(
        query(), [AllowedDocument(101, 2)]
    )

    # 历史行为：legacy point 没有 fingerprint 仍然可用。
    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [11]


async def test_switch_false_still_rejects_explicit_mismatch() -> None:
    store = FakeStore(
        [
            hit(
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=OTHER,
            )
        ]
    )

    result = await service(store, require=False).search(
        query(), [AllowedDocument(101, 2)]
    )

    # 历史行为：显式记录了不同 fingerprint 的 point 一律拒绝。
    assert result.chunks == []
    assert result.status is KnowledgeStatus.DEGRADED


async def test_switch_false_keeps_version_filtering_unchanged() -> None:
    store = FakeStore(
        [
            hit(
                chunk_id=11,
                index_version=1,
                doc_version=doc_version_for(101, 1),
                embedding_fingerprint=CURRENT,
            ),
            hit(
                chunk_id=12,
                index_version=2,
                doc_version=doc_version_for(101, 2),
                embedding_fingerprint=CURRENT,
            ),
        ]
    )

    result = await service(store, require=False).search(
        query(), [AllowedDocument(101, 2)]
    )

    assert result.status is KnowledgeStatus.SUCCESS
    assert [chunk.id for chunk in result.chunks] == [12]
