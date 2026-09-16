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
from app.retriever.base import RetrievalError, Retriever
from app.retriever.fixture import FixtureRetriever
from app.retriever.http_backend import HttpBackendRetriever
from app.schemas import Concept, EvidenceItem, KnowledgeChunk, MemoryItem, Understanding

log = logging.getLogger(__name__)

MODE_AUTO = "auto"
MODE_HTTP = "http"
MODE_FIXTURE = "fixture"


def build_retriever(settings: Settings, auth_token: str | None) -> Retriever:
    """按配置与本次请求组装检索源。每次运行一个实例，用完即关。"""
    mode = settings.retriever_mode.strip().lower()
    fixture = _build_fixture(settings)

    if mode == MODE_FIXTURE:
        return fixture

    if not auth_token:
        if mode == MODE_HTTP:
            raise RetrievalError(
                "AILS_RETRIEVER_MODE=http 但请求里没有 authToken。"
                "HTTP 检索要带用户自己的 JWT，因为后端读接口按登录用户鉴权。"
            )
        log.info("未传 authToken，使用 Fixture 检索源")
        return fixture

    http = HttpBackendRetriever(
        settings.backend_base_url,
        auth_token,
        timeout_seconds=settings.backend_timeout_seconds,
        memory_top_k=settings.memory_top_k,
        knowledge_top_k=settings.knowledge_top_k,
    )
    if mode == MODE_HTTP:
        return http

    if mode != MODE_AUTO:
        raise ValueError(f"未知的 AILS_RETRIEVER_MODE：{settings.retriever_mode}（可选：auto / http / fixture）")

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

    async def get_knowledge(
        self, space_id: int, concept_ids: list[int], query: str
    ) -> list[KnowledgeChunk]:
        return await self._call(
            "get_knowledge", space_id=space_id, concept_ids=concept_ids, query=query
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


def _build_fixture(settings: Settings) -> FixtureRetriever:
    return FixtureRetriever(
        settings.fixture_file,
        memory_top_k=settings.memory_top_k,
        knowledge_top_k=settings.knowledge_top_k,
    )
