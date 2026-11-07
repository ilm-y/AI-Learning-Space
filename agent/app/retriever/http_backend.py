"""HTTP Retriever —— 调用后端已有的**只读**接口，读真实的跨对话认知数据。

用到的都是后端已经写实、且天然按登录用户鉴权的接口：

| 检索线 | 后端接口 |
| --- | --- |
| Concept | `GET /api/spaces/{spaceId}/concepts` |
| Understanding | `GET /api/understandings/{conceptId}` |
| Memory | `GET /api/memories/search?spaceId=&conceptId=&keyword=` |
| Evidence | `GET /api/understandings/{conceptId}/evidence` |
| Knowledge 白名单 | `GET /api/spaces/{spaceId}/documents/ready-ids?embeddingModel=` |
| Knowledge 向量 | 现有 Embedding Provider + Qdrant（`KnowledgeSearchService`） |

**为什么不自己连 MySQL？** 因为 `agent-architecture.md` 第二节写得很清楚：
业务数据由 Backend 管理，认知判断由 Agent 管理，Agent 不绕过 Backend 直接改库。
读也走同一扇门，这样权限、软删除、状态过滤（比如 Memory 只认 ACTIVE）这些规则
永远只有一份实现，不会在 Agent 里长出第二套。

**鉴权**：这些接口按登录用户鉴权，有两种调用方式：

1. 同步 Chat / HTTP Analyze 链路：透传用户自己的 JWT（`Authorization: Bearer ...`）；
2. 异步 MQ Worker：不带用户 JWT（它可能已经过期），改用服务端共享凭据
   `X-Internal-Token` + `X-Internal-User-Id`。后端过滤器校验后以该用户身份鉴权，
   业务侧的归属与空间校验仍照常执行。

**失败策略**：连不上后端是「环境问题」，抛 `RetrievalError` 让上层决定降级；
单个 Concept 查不到是「正常情况」（用户还没学过这个知识点），记日志后跳过。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.retriever.base import (
    KnowledgeQuery,
    KnowledgeResult,
    RetrievalError,
)
from app.retriever.knowledge import AllowedDocument, KnowledgeSearchService
from app.schemas import (
    ApiResponse,
    Concept,
    EvidenceItem,
    MemoryItem,
    Understanding,
    UnderstandingRevision,
)

log = logging.getLogger(__name__)


# 内部服务凭据的请求头。与后端 InternalServiceAuthenticationFilter 的常量保持一致。
INTERNAL_TOKEN_HEADER = "X-Internal-Token"
INTERNAL_USER_HEADER = "X-Internal-User-Id"
INTERNAL_IDENTITY_HEADER = "X-Internal-Identity"


class HttpBackendRetriever:
    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        *,
        internal_token: str | None = None,
        internal_identity: str | None = None,
        user_id: int | None = None,
        timeout_seconds: float = 10.0,
        memory_top_k: int = 5,
        knowledge_top_k: int = 5,
        knowledge_search: KnowledgeSearchService | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._memory_top_k = memory_top_k
        self._knowledge_top_k = knowledge_top_k
        self._knowledge_search = knowledge_search
        # 二选一：用户 JWT（同步链路）或服务端凭据（异步 Worker）。
        # 两者都没有时直接失败，而不是发一个注定 401 的请求。
        if auth_token:
            headers = {"Authorization": f"Bearer {auth_token}"}
        elif internal_token and internal_identity and user_id is not None:
            headers = {
                INTERNAL_TOKEN_HEADER: internal_token,
                INTERNAL_IDENTITY_HEADER: internal_identity,
                INTERNAL_USER_HEADER: str(user_id),
            }
        else:
            raise ValueError(
                "HttpBackendRetriever 需要 auth_token，或 "
                "internal_token + internal_identity + user_id"
            )
        self._client = httpx.AsyncClient(timeout=timeout_seconds, headers=headers)

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
        #
        # conceptId 传本轮命中的第一个概念：后端的可见性规则里，CONCEPT 级记忆只有在
        # 「spaceId 与 conceptId 同时给对」时才可见。不传的话它们会被静默排除 ——
        # 结果依然是个正常返回的空列表，看不出任何异常。取第一个而不是全部，是因为
        # 后端一个请求只认一个 conceptId；本轮的首要话题就是第一个命中的概念。
        params: dict[str, Any] = {"spaceId": space_id, "keyword": query[:60]}
        if concept_ids:
            params["conceptId"] = concept_ids[0]
        data = await self._get(
            "/api/memories/search",
            params=params,
            fatal=False,
        )
        return _parse_list(data, MemoryItem)[: self._memory_top_k]

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        # 第一步必须是后端授权白名单。拿不到就 fail closed；绝不先查 Qdrant 再过滤，
        # 也不把白名单失败退化成 Fixture 或未过滤搜索。
        if self._knowledge_search is None:
            return KnowledgeResult.degraded(
                "知识检索组件未配置，本次回答未使用文档资料。"
            )

        params = {
            "embeddingModel": self._knowledge_search.embedding_model,
            "embeddingProvider": self._knowledge_search.embedding_provider,
            "embeddingDimension": self._knowledge_search.embedding_dimension,
            "embeddingFingerprint": self._knowledge_search.embedding_fingerprint,
        }
        try:
            data = await self._get(
                f"/api/spaces/{query.space_id}/documents/ready-ids",
                params=params,
                fatal=False,
            )
        except RetrievalError as exc:
            log.warning("文档白名单获取失败，知识检索 fail closed：%s", exc)
            return KnowledgeResult.degraded(
                "无法确认可检索文档的权限，本次回答未使用文档资料。"
            )

        if data is None:
            return KnowledgeResult.degraded(
                "文档白名单获取失败，本次回答未使用文档资料。"
            )

        allowed = _parse_ready_ids(data)
        if allowed is None:
            log.warning("文档白名单格式异常，知识检索 fail closed")
            return KnowledgeResult.degraded(
                "文档白名单数据不完整，本次回答未使用文档资料。"
            )

        try:
            return await self._knowledge_search.search(query, allowed)
        except Exception as exc:  # noqa: BLE001 —— 普通检索故障不得打挂对话
            log.warning("知识检索失败，降级为空：%s", type(exc).__name__)
            return KnowledgeResult.degraded(
                "知识检索依赖不可用，本次回答未使用文档资料。"
            )

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

    async def get_revisions(
        self, user_id: int, concept_ids: list[int]
    ) -> dict[int, list[UnderstandingRevision]]:
        """读取每个概念的真实版本历史。

        没有快照/没有历史是正常情况（返回空列表），不抛 RetrievalError。
        后端接口本身按登录用户鉴权，这里不自行推断权限。
        """
        results: dict[int, list[UnderstandingRevision]] = {}
        for concept_id in concept_ids:
            data = await self._get(
                f"/api/understandings/{concept_id}/revisions", fatal=False
            )
            revisions = _parse_list(data, UnderstandingRevision)
            revisions.sort(key=lambda item: item.revision_no)
            results[concept_id] = revisions
        return results

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ) -> list[EvidenceItem]:
        """按 Evidence id 精确回查，用于复盘时还原 trigger/reversal 证据。

        单条读取是为了复用已有的详情接口（含 sources），不再新增后端查询；
        条数由上游按 Revision 关联裁剪，通常只有个位数。
        """
        wanted_concepts = set(concept_ids)
        results: list[EvidenceItem] = []
        seen: set[int] = set()
        for evidence_id in evidence_ids:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            data = await self._get(f"/api/evidence/{evidence_id}", fatal=False)
            item = _parse_one(data, EvidenceItem)
            # 详情接口按用户鉴权，但仍校验它确实属于本轮概念，避免把别的概念
            # （即使同属该用户）的证据混进当前轨迹。
            if item is not None and item.concept_id in wanted_concepts:
                results.append(item)
        results.sort(key=lambda item: item.created_at or datetime.min)
        return results

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
            raise RetrievalError(
                "后端返回 401：凭据无效（用户 JWT 过期，或 AILS_INTERNAL_TOKEN 与后端不一致）"
            )
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


def _parse_ready_ids(data: Any) -> list[AllowedDocument] | None:
    """解析后端 ready-ids，任何一条不合法都整体拒绝，避免部分白名单。

    兼容两种返回：
    * 新契约 `[{"id": 1, "indexVersion": 2}]`；
    * 旧契约 `[1, 2]`（迁移期/旧后端），视为 indexVersion=0 的 legacy 文档。
    """
    if not isinstance(data, list):
        return None
    result: list[AllowedDocument] = []
    seen: set[int] = set()
    for value in data:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            if value <= 0:
                return None
            document_id, index_version = value, 0
        elif isinstance(value, dict):
            document_id = value.get("id")
            index_version = value.get("indexVersion", 0)
            if (
                isinstance(document_id, bool)
                or not isinstance(document_id, int)
                or document_id <= 0
            ):
                return None
            if (
                isinstance(index_version, bool)
                or not isinstance(index_version, int)
                or index_version < 0
            ):
                return None
        else:
            return None
        if document_id in seen:
            # 后端白名单按 id 唯一；重复说明状态异常，fail closed。
            return None
        seen.add(document_id)
        result.append(
            AllowedDocument(document_id=document_id, index_version=index_version)
        )
    return result
