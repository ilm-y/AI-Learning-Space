"""检索源的路由与降级。

选择逻辑只有三条规则，说清楚比写得聪明重要：

```text
AILS_RETRIEVER_MODE = http     → 只用真实后端；连不上就报错，不偷偷用假数据
AILS_RETRIEVER_MODE = fixture  → 只用本地样例数据
AILS_RETRIEVER_MODE = auto     → 请求里带 authToken 就用后端，否则用 Fixture；
                                 后端连不上时降级到 Fixture，但**留下轨迹**
```

为什么 auto 要留轨迹：降级本身是合理的（后端没起来不该让 Agent 完全不能演示），
但如果降级发生得静默无声，人就会以为「跨对话召回不生效」是 Agent 的 bug。
所以每次降级都记一条 `RETRIEVE` 事件，并在日志里打 WARNING。
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.retriever.base import (
    KnowledgeQuery,
    KnowledgeResult,
    RetrievalError,
    Retriever,
)
from app.retriever.fixture import FixtureRetriever
from app.retriever.http_backend import HttpBackendRetriever
from app.retriever.knowledge import KnowledgeSearchService
from app.schemas import Concept, EvidenceItem, MemoryItem, Understanding

log = logging.getLogger(__name__)

MODE_AUTO = "auto"
MODE_HTTP = "http"
MODE_FIXTURE = "fixture"


def build_retriever(
    settings: Settings,
    auth_token: str | None,
    *,
    allow_degradation: bool = True,
    user_id: int | None = None,
    knowledge_search: KnowledgeSearchService | None = None,
) -> Retriever:
    """按配置与本次请求组装检索源。每次运行一个实例，用完即关。

    凭据二选一：
    - ``auth_token``：同步 Chain 透传的用户 JWT；
    - ``settings.internal_token`` + ``user_id``：异步 Worker 的服务端凭据。

    :param allow_degradation: 允许 ``auto`` 模式在无凭据 / 后端不可用时退回 Fixture。
        Chat 链路保持默认（演示优先）；**Analyze 链路必须传 False** ——
        分析结果会被写进真实用户的长期认知，拿样例数据顶替会直接污染它。
    """
    mode = settings.retriever_mode.strip().lower()
    fixture = _build_fixture(settings)

    if mode == MODE_FIXTURE:
        return fixture

    http = _build_http(settings, auth_token, user_id, knowledge_search)
    if http is None:
        if not allow_degradation:
            raise RetrievalError(
                "分析链路需要 authToken（或 AILS_INTERNAL_TOKEN）才能走 HTTP 检索；"
                "不会退回 Fixture，否则样例概念会被写进真实用户的长期认知。"
            )
        if mode == MODE_HTTP:
            raise RetrievalError(
                "AILS_RETRIEVER_MODE=http 但既没有 authToken，也没有 AILS_INTERNAL_TOKEN + userId。"
                "后端读接口按登录用户鉴权，必须带其中一种凭据。"
            )
        log.info("未提供检索凭据，使用 Fixture 检索源")
        return fixture

    if mode == MODE_HTTP:
        return http

    if mode != MODE_AUTO:
        raise ValueError(f"未知的 AILS_RETRIEVER_MODE：{settings.retriever_mode}（可选：auto / http / fixture）")

    if not allow_degradation:
        # 分析链路：auto 也只认真实后端。检索失败会作为 RetrievalError 上抛，
        # 由后端记录后回退 rules-v1 —— 这比拿 Fixture 默默生成一份假分析安全得多。
        return http

    return FallbackRetriever(http, fixture)


class FallbackRetriever:
    """HTTP 为主，Fixture 兜底。降级会写进日志，不静默。"""

    def __init__(self, primary: Retriever, fallback: Retriever) -> None:
        self._primary = primary
        self._fallback = fallback
        self.degraded = False

    @property
    def name(self) -> str:
        return self._primary.name

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return await self._call("list_concepts", space_id=space_id)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        return await self._call(
            "get_understandings", user_id=user_id, space_id=space_id, concept_ids=concept_ids
        )

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        return await self._call(
            "get_memories", user_id=user_id, space_id=space_id, concept_ids=concept_ids, query=query
        )

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        # 知识检索是唯一不能「降级到 Fixture」的线：Fixture 片段没有经过后端
        # 文档白名单与 Qdrant 权限过滤，拿它顶替会向用户展示不存在的资料来源。
        # 因此即使本次运行已经在别的检索线上降级，Knowledge 也只返回空 + warning。
        if self.degraded:
            return KnowledgeResult.degraded(
                "检索源已降级，知识检索保持为空且不回退样例数据。"
            )
        try:
            return await self._primary.get_knowledge(query)
        except RetrievalError as exc:
            log.warning("知识检索不可用，保持空知识（不回退 Fixture）：%s", exc)
            return KnowledgeResult.degraded(
                "知识检索源不可用，本次回答未使用文档资料。"
            )

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        return await self._call("get_evidences", user_id=user_id, concept_ids=concept_ids, limit=limit)

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()

    # ---------- 内部 ----------

    async def _call(self, method: str, **kwargs):
        if self.degraded:
            return await getattr(self._fallback, method)(**kwargs)
        try:
            return await getattr(self._primary, method)(**kwargs)
        except RetrievalError as exc:
            log.warning("后端检索不可用，本次运行降级到 Fixture：%s", exc)
            self.degraded = True
            return await getattr(self._fallback, method)(**kwargs)


def _build_http(
    settings: Settings,
    auth_token: str | None,
    user_id: int | None,
    knowledge_search: KnowledgeSearchService | None,
) -> HttpBackendRetriever | None:
    """有凭据才建 HTTP 检索源。用户 JWT 优先于服务端凭据。"""
    if auth_token:
        return HttpBackendRetriever(
            settings.backend_base_url,
            auth_token,
            timeout_seconds=settings.backend_timeout_seconds,
            memory_top_k=settings.memory_top_k,
            knowledge_top_k=settings.knowledge_top_k,
            knowledge_search=knowledge_search,
        )
    if settings.internal_token and user_id is not None:
        return HttpBackendRetriever(
            settings.backend_base_url,
            internal_token=settings.internal_token,
            user_id=user_id,
            timeout_seconds=settings.backend_timeout_seconds,
            memory_top_k=settings.memory_top_k,
            knowledge_top_k=settings.knowledge_top_k,
            knowledge_search=knowledge_search,
        )
    return None


def _build_fixture(settings: Settings) -> FixtureRetriever:
    return FixtureRetriever(
        settings.fixture_file,
        memory_top_k=settings.memory_top_k,
        knowledge_top_k=settings.knowledge_top_k,
    )
