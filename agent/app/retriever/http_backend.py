"""HTTP Retriever —— 调用后端已有的**只读**接口，读真实的跨对话认知数据。

用到的都是后端已经写实、且天然按登录用户鉴权的接口：

| 检索线 | 后端接口 |
| --- | --- |
| Concept | `GET /api/spaces/{spaceId}/concepts` |
| Understanding | `GET /api/understandings/{conceptId}` |
| Memory | `GET /api/memories/search?spaceId=&keyword=` |
| Evidence | `GET /api/understandings/{conceptId}/evidence` |
| Knowledge | 无 —— 文档接口还是 501，第一版固定返回空 |

**为什么不自己连 MySQL？** 因为 `agent-architecture.md` 第二节写得很清楚：
业务数据由 Backend 管理，认知判断由 Agent 管理，Agent 不绕过 Backend 直接改库。
读也走同一扇门，这样权限、软删除、状态过滤（比如 Memory 只认 ACTIVE）这些规则
永远只有一份实现，不会在 Agent 里长出第二套。

**鉴权**：这些接口按登录用户鉴权，所以必须把用户自己的 JWT 透传进来。
后端接入时把 token 放进 `ChatAgentRequest.authToken` 即可，Agent 侧不需要改代码。

**失败策略**：连不上后端是「环境问题」，抛 `RetrievalError` 让上层决定降级；
单个 Concept 查不到是「正常情况」（用户还没学过这个知识点），记日志后跳过。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.retriever.base import RetrievalError
from app.schemas import (
    ApiResponse,
    Concept,
    EvidenceItem,
    KnowledgeChunk,
    MemoryItem,
    Understanding,
)

log = logging.getLogger(__name__)


class HttpBackendRetriever:
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        *,
        timeout_seconds: float = 10.0,
        memory_top_k: int = 5,
        knowledge_top_k: int = 5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._memory_top_k = memory_top_k
        self._knowledge_top_k = knowledge_top_k
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    @property
    def name(self) -> str:
        return f"http:{self._base_url}"

    # ---------- 检索 ----------

    async def list_concepts(self, space_id: int) -> list[Concept]:
        data = await self._get(f"/api/spaces/{space_id}/concepts")
        return _parse_list(data, Concept)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        results: list[Understanding] = []
        for concept_id in concept_ids:
            # data 为 null 表示这个用户还没有该 Concept 的快照，是正常情况
            data = await self._get(f"/api/understandings/{concept_id}", fatal=False)
            item = _parse_one(data, Understanding)
            if item is not None:
                results.append(item)
        return results

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        # 后端已按 status = ACTIVE 过滤，CANDIDATE 不会流进来。
        #
        # keyword 传的是用户原话（截前 60 字），这没问题 —— 后端的 /api/memories/search
        # 不是把整串当作一个子串去 LIKE，而是先按空白/标点切词、中文再补字符二元组，
        # 命中任意一个词就算候选。所以「一句话」正是它期待的输入形态。
        # 反过来，如果哪天后端改回「整串当子串」，这里传原话就会永远返回空列表，
        # 而空列表和不报错长得一模一样 —— 排查时先看后端 search 的实现。
        data = await self._get(
            "/api/memories/search",
            params={"spaceId": space_id, "keyword": query[:60]},
            fatal=False,
        )
        return _parse_list(data, MemoryItem)[: self._memory_top_k]

    async def get_knowledge(
        self, space_id: int, concept_ids: list[int], query: str
    ) -> list[KnowledgeChunk]:
        # 文档 / 向量检索是 M6，后端对应接口当前返回 501。这里如实返回空，
        # 而不是退回关键词在内存里假装检索 —— 上层已经能处理「没有知识」。
        return []

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        if limit <= 0:
            return []
        results: list[EvidenceItem] = []
        for concept_id in concept_ids:
            data = await self._get(f"/api/understandings/{concept_id}/evidence", fatal=False)
            results.extend(_parse_list(data, EvidenceItem))
        # 后端每个 concept 各返回一段**已按时间倒序**（最新在前）的列表，但把多个 concept
        # 的结果首尾拼起来之后，整体就不再有序了 —— 所以不能靠切片位置去取「最近」，
        # 必须自己在 Python 侧按 createdAt 重排一次，再取前 limit 条。
        #
        # 这里踩过一次坑：原来写的是 results[-limit:]，注释还写着「后端按时间正序返回」。
        # 后端其实是倒序，于是 [-limit:] 取到的是**最旧**的那几条 ——
        # 列表看着有内容、也不报错，但 Tutor 拿到的是用户最早的证据，
        # 之后所有的学习进展它一概看不见。方向反了的 bug 不会崩，只会一直安静地答错。
        results.sort(
            key=lambda item: item.created_at or datetime.min,
            reverse=True,
        )
        return results[:limit]

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- 内部 ----------

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None, fatal: bool = True
    ) -> Any:
        """发一次请求并拆掉统一响应体。

        `fatal=True` 的调用（列 Concept）失败说明后端整体不可用，向上抛；
        `fatal=False` 的调用失败只影响这一次检索的丰满度，记日志后返回 None。
        """
        try:
            response = await self._client.get(f"{self._base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            if fatal:
                raise RetrievalError(f"连接后端失败：{self._base_url}{path}（{exc}）") from exc
            log.warning("检索请求失败，跳过：%s（%s）", path, exc)
            return None

        if response.status_code == 401:
            raise RetrievalError("后端返回 401：透传的用户 JWT 无效或已过期")
        if response.status_code >= 400:
            if fatal:
                raise RetrievalError(f"后端返回 {response.status_code}：{path}")
            log.warning("检索请求返回 %s，跳过：%s", response.status_code, path)
            return None

        try:
            envelope = ApiResponse.model_validate(response.json())
        except ValueError:
            log.warning("响应不是统一响应体，跳过：%s", path)
            return None
        if not envelope.is_ok():
            if fatal:
                raise RetrievalError(f"后端业务错误 code={envelope.code}：{envelope.message}")
            log.warning("后端业务错误，跳过：%s code=%s", path, envelope.code)
            return None
        return envelope.data


def _parse_list(data: Any, model: type) -> list:
    if not isinstance(data, list):
        return []
    parsed = []
    for item in data:
        try:
            parsed.append(model.model_validate(item))
        except ValueError:
            # 后端字段变更时宁可少一条，也不要整次对话挂掉
            log.warning("跳过无法解析的 %s：%s", model.__name__, str(item)[:200])
    return parsed


def _parse_one(data: Any, model: type):
    if not isinstance(data, dict):
        return None
    try:
        return model.model_validate(data)
    except ValueError:
        log.warning("跳过无法解析的 %s：%s", model.__name__, str(data)[:200])
        return None
