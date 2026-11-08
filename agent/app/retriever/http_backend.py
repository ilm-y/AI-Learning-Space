"""HTTP Retriever —— 调用后端已有的**只读**接口，读真实的跨对话认知数据。

用到的都是后端已经写实、且天然按登录用户鉴权的接口：

| 检索线 | 后端接口 |
| --- | --- |
| Concept | `GET /api/spaces/{spaceId}/concepts` |
| Understanding | `GET /api/understandings/{conceptId}` |
| Memory | `GET /api/memories/search/hybrid?spaceId=&conceptId=&keyword=` |
| Evidence | `GET /api/evidences?spaceId=&conceptIds=&limit=`（有界批量；旧单概念接口保留兼容） |
| Knowledge 白名单 | `GET /api/spaces/{spaceId}/documents/ready-ids?embeddingModel=&embeddingFingerprint=` |
| Knowledge 向量 | 现有 Embedding Provider + Qdrant（`KnowledgeSearchService`） |

**诊断接口不在这里消费。** 后端的
`GET /api/spaces/{spaceId}/documents/diagnostics/legacy` 只供管理员/owner 排查与
RAG-04 阶段 B 重建清单使用；Agent 的正常检索只走 `ready-ids`，且严格模式下
必须携带当前 `embeddingFingerprint`。两条路径刻意分开，避免诊断结果被当成
检索资格判定。

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

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    MemoryRetrievalResult,
    RelatedConceptResult,
    RelatedConceptStatus,
    RetrievalError,
    RetrievalStatus,
    RevisionRetrievalResult,
    UnderstandingRetrievalResult,
)
from app.retriever.knowledge import AllowedDocument, KnowledgeSearchService
from app.schemas import (
    ApiResponse,
    Concept,
    EvidenceItem,
    MemoryItem,
    RelatedConcept,
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
    ) -> UnderstandingRetrievalResult:
        """逐个 concept 读取当前认知快照，并保留「失败 ≠ 没有快照」的区分。

        后端对「还没有该 Concept 的快照」返回 ``data: null``（正常），而超时、5xx、
        业务错误或响应无法解析属于查询失败。两者绝不能都变成「这个用户没学过」：
        前者计入 EMPTY，后者让整体状态变成 DEGRADED（已成功取到的快照仍保留）。
        """
        results: list[Understanding] = []
        failed: list[int] = []
        for concept_id in concept_ids:
            ok, data, failure = await self._get_outcome(
                f"/api/understandings/{concept_id}"
            )
            if not ok:
                log.warning(
                    "学习认知查询失败：concept=%s reason=%s", concept_id, failure
                )
                failed.append(concept_id)
                continue
            if data is None:
                # 后端明确返回 null：该用户还没有这个 Concept 的快照，是正常空结果。
                continue
            item = _parse_one(data, Understanding)
            if item is None:
                failed.append(concept_id)
                continue
            results.append(item)

        if failed:
            return UnderstandingRetrievalResult(
                items=results,
                status=RetrievalStatus.DEGRADED,
                warning=(
                    f"学习认知检索失败或不完整（conceptIds={failed}），"
                    "无法确认这些知识点是否已有认知记录。"
                ),
                failed_concept_ids=failed,
            )
        return UnderstandingRetrievalResult(
            items=results,
            status=RetrievalStatus.SUCCESS if results else RetrievalStatus.EMPTY,
        )

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> MemoryRetrievalResult:
        """读取与本轮概念相关的长期记忆。

        **多概念限制**：后端 `/api/memories/search/hybrid` 的 `conceptId` 是单个
        `Long`，一次请求只能覆盖一个概念。D4-2 的 Policy 会在多概念问题时给出
        完整的 `memory_concept_ids`，这里按概念逐次请求后合并，保证每个概念都有
        候选进入结果；不足是 N 次往返、且没有跨概念的统一 RRF 排序。在不改后端
        接口/表结构的前提下，这是能同时满足「CONCEPT 级记忆可见」与「多概念不丢」
        的最小实现。单概念仍然是原来的一次请求，参数与状态语义完全不变。
        """
        ordered_ids = list(dict.fromkeys(int(item) for item in concept_ids))
        if len(ordered_ids) <= 1:
            return await self._search_memories(
                space_id, ordered_ids[0] if ordered_ids else None, query
            )

        results = await asyncio.gather(
            *(
                self._search_memories(space_id, concept_id, query)
                for concept_id in ordered_ids
            )
        )
        return _merge_memory_results(results, ordered_ids, self._memory_top_k)

    async def _search_memories(
        self, space_id: int, concept_id: int | None, query: str
    ) -> MemoryRetrievalResult:
        # 后端已按 status = ACTIVE 过滤，CANDIDATE 不会流进来。
        #
        # 走 /search/hybrid 而不是旧 /search：旧端点只返回 List，检索整条链路
        # 不可用时和「确实没有相关记忆」都是空列表，Agent 无从区分。混合检索端点
        # 带 status（OK / DEGRADED_* / UNAVAILABLE / BLANK_QUERY），必须在这里
        # 显式识别并保留状态，绝不能把「检索失败」静默当成「没有记忆」。
        #
        # keyword 传的是用户原话（截前 60 字），这没问题 —— 后端不是把整串当作
        # 一个子串去 LIKE，而是先按空白/标点切词、中文再补字符二元组，命中任意
        # 一个词就算候选。所以「一句话」正是它期待的输入形态。
        #
        # conceptId：后端的可见性规则里，CONCEPT 级记忆只有在「spaceId 与
        # conceptId 同时给对」时才可见。不传的话它们会被静默排除 —— 结果依然是
        # 个正常返回的空列表，看不出任何异常。
        params: dict[str, Any] = {
            "spaceId": space_id,
            "keyword": query[:60],
            "limit": self._memory_top_k,
        }
        if concept_id is not None:
            params["conceptId"] = concept_id
        ok, data, failure = await self._get_outcome(
            "/api/memories/search/hybrid", params=params
        )
        if not ok:
            log.warning("记忆检索请求失败：space=%s reason=%s", space_id, failure)
            return MemoryRetrievalResult(
                status=RetrievalStatus.DEGRADED,
                detail="REQUEST_FAILED",
                warning="记忆检索请求失败，无法确认是否有相关记忆。",
            )
        if data is None:
            # 请求成功但响应体没有 data：不能解释成「确实没有记忆」。
            return MemoryRetrievalResult(
                status=RetrievalStatus.DEGRADED,
                detail="EMPTY_RESPONSE",
                warning="记忆检索响应缺少数据，无法确认是否有相关记忆。",
            )

        status, memories, warnings = _parse_memory_search(data)
        joined = "；".join(warnings)
        trimmed = memories[: self._memory_top_k]

        if status == "OK":
            final = RetrievalStatus.SUCCESS if trimmed else RetrievalStatus.EMPTY
            return MemoryRetrievalResult(items=trimmed, status=final, detail=status)
        if status in {"DEGRADED_VECTOR", "DEGRADED_KEYWORD"}:
            log.warning("记忆检索降级（status=%s）：%s", status, joined or "无附加信息")
            return MemoryRetrievalResult(
                items=trimmed,
                status=RetrievalStatus.DEGRADED,
                detail=status,
                warning=f"记忆检索部分降级（{status}），结果可能不完整；不能据此判断用户没有相关记忆。",
            )
        if status == "UNAVAILABLE":
            log.warning("记忆检索不可用（status=UNAVAILABLE）：%s", joined or "无附加信息")
            return MemoryRetrievalResult(
                status=RetrievalStatus.DEGRADED,
                detail=status,
                warning="记忆检索不可用，无法确认是否有相关记忆。",
            )
        if status == "BLANK_QUERY":
            # 后端因关键词为空而未做语义检索；即使返回了列表也不是「按话题召回」，
            # 空列表更不能解释成「没有记忆」。
            return MemoryRetrievalResult(
                items=trimmed,
                status=(
                    RetrievalStatus.SUCCESS if trimmed else RetrievalStatus.DEGRADED
                ),
                detail=status,
                warning=(
                    None
                    if trimmed
                    else "记忆检索未执行语义查询（BLANK_QUERY），无法确认是否有相关记忆。"
                ),
            )

        # status 缺失（旧格式 / 网关裁剪）：结果可能可用，但完整性无法确认。
        log.warning("记忆检索响应缺少可识别 status，按不完整结果处理")
        return MemoryRetrievalResult(
            items=trimmed,
            status=RetrievalStatus.DEGRADED,
            detail=status or "UNKNOWN",
            warning="记忆检索响应缺少状态，无法确认结果是否完整；不能据此判断用户没有相关记忆。",
        )

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        # 第一步必须是后端授权白名单。拿不到就 fail closed；绝不先查 Qdrant 再过滤，
        # 也不把白名单失败退化成 Fixture 或未过滤搜索。
        if self._knowledge_search is None:
            return KnowledgeResult.degraded(
                "知识检索组件未配置，本次回答未使用文档资料。"
            )

        # Runtime 调用契约（RAG-04 阶段 A）：严格模式下，正常检索必须携带当前
        # fingerprint。没有任何“model-only 正常检索”的旁路；诊断查询是后端独立
        # 端点，Agent 不调用、也不消费其结果。
        current_fingerprint = self._knowledge_search.embedding_fingerprint
        if (
            self._knowledge_search.require_embedding_fingerprint
            and not current_fingerprint.strip()
        ):
            log.warning("严格指纹模式缺少当前 fingerprint，知识检索 fail closed")
            return KnowledgeResult.degraded(
                "严格指纹模式要求当前 Embedding 配置具有非空 fingerprint，"
                "本次未使用文档资料。"
            )

        params = {
            "embeddingModel": self._knowledge_search.embedding_model,
            "embeddingProvider": self._knowledge_search.embedding_provider,
            "embeddingDimension": self._knowledge_search.embedding_dimension,
            "embeddingFingerprint": current_fingerprint,
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
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        """一次有界批量查询：服务端按 created_at / id 倒序 LIMIT，并批量加载 sources。

        不再逐概念请求、也不在应用层「全量拉取后切片」：
        - 服务端封装了 `spaceId + conceptIds + limit`，并按用户/空间校验概念归属；
        - 一次请求覆盖原始概念与跨概念扩展，避免 N 次往返；
        - 失败返回 DEGRADED + warning，成功但无数据才是 EMPTY。
        """
        if limit <= 0 or not concept_ids:
            return EvidenceRetrievalResult()
        # 去重但保序：重复 conceptId 只会让服务端 IN 列表变长，不改变语义。
        ordered_ids = list(dict.fromkeys(int(item) for item in concept_ids))
        params: dict[str, Any] = {
            "spaceId": space_id,
            "conceptIds": ",".join(str(item) for item in ordered_ids),
            "limit": limit,
        }
        try:
            data = await self._get("/api/evidences", params=params, fatal=False)
        except RetrievalError as exc:
            # _get 对 401 始终抛错，即使 fatal=False。证据检索失败不能打挂整轮，
            # 但必须显式降级，让上层知道「这不是没有历史证据」。
            warning = (
                f"证据检索失败（spaceId={space_id}, conceptIds={ordered_ids}）："
                f"{exc}"
            )
            log.warning(warning)
            return EvidenceRetrievalResult.degraded(warning)

        if data is None:
            warning = (
                f"证据检索失败（spaceId={space_id}, conceptIds={ordered_ids}）："
                "后端未返回可用数据。"
            )
            log.warning(warning)
            return EvidenceRetrievalResult.degraded(warning)

        if not isinstance(data, list):
            warning = (
                f"证据检索响应格式异常（spaceId={space_id}, conceptIds={ordered_ids}）："
                f"{str(data)[:120]}"
            )
            log.warning(warning)
            return EvidenceRetrievalResult.degraded(warning)

        items = _parse_list(data, EvidenceItem)
        if len(items) < len(data):
            # 有数据但部分无法解析：已解析的仍然可用，但整体必须标成降级，
            # 不能把「解析失败」伪装成「这些证据不存在」。
            warning = (
                f"证据检索结果不完整（spaceId={space_id}, conceptIds={ordered_ids}）："
                f"{len(data) - len(items)} 条无法解析，已保留 {len(items)} 条。"
            )
            log.warning(warning)
            return EvidenceRetrievalResult.degraded(warning, items=items)
        return EvidenceRetrievalResult.from_items(items)

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        """查询一轮识别概念的一跳相关概念。

        只在 Evidence 检索被触发时由 Runtime 调用。每个概念一次请求，客户端去重；
        任一请求失败都会体现在 status / warning 上，**不会**退化成“没有关系”。
        后端接口本身按登录用户 + 空间校验，Agent 不自行推断权限。
        """
        if limit <= 0 or not concept_ids:
            return RelatedConceptResult(status=RelatedConceptStatus.EMPTY)

        types = [item for item in relation_types if item]
        if not types:
            return RelatedConceptResult(status=RelatedConceptStatus.EMPTY)

        params: dict[str, Any] = {
            "types": ",".join(types),
            "limit": limit,
        }
        items: list[RelatedConcept] = []
        failures: list[int] = []
        successes = 0
        seen: set[int] = set()

        for concept_id in concept_ids:
            path = f"/api/spaces/{space_id}/concepts/{concept_id}/relations"
            try:
                data = await self._get(path, params=params, fatal=False)
            except RetrievalError as exc:
                # _get 对 401 始终抛错，即使 fatal=False。这里必须自己兜住：
                # 相关概念查询失败不能影响原始概念的 Evidence 检索。
                log.warning(
                    "相关概念查询失败：space=%s concept=%s error=%s",
                    space_id, concept_id, exc,
                )
                failures.append(concept_id)
                continue

            if data is None or not isinstance(data, list):
                # _get 已在 HTTP / 业务错误时打过日志；这里补一条带上下文的 warning。
                log.warning(
                    "相关概念查询失败或响应格式异常：space=%s concept=%s",
                    space_id, concept_id,
                )
                failures.append(concept_id)
                continue

            successes += 1
            for raw in data:
                item = _parse_one(raw, RelatedConcept)
                if item is None:
                    continue
                if item.concept_id <= 0 or item.concept_id == concept_id:
                    continue
                if item.relation_type not in types:
                    continue
                if item.concept_id in seen:
                    continue
                seen.add(item.concept_id)
                items.append(item.model_copy(update={"source_concept_id": concept_id}))

        if failures and successes == 0:
            status = RelatedConceptStatus.FAILED
        elif failures:
            status = RelatedConceptStatus.PARTIAL
        elif items:
            status = RelatedConceptStatus.OK
        else:
            status = RelatedConceptStatus.EMPTY

        warning = None
        if failures:
            warning = (
                f"相关概念查询失败（space={space_id}, conceptIds={failures}），"
                "本次未做跨概念 Evidence 扩展。"
            )
            log.warning(warning)

        return RelatedConceptResult(items=items, status=status, warning=warning)

    async def get_revisions(
        self, user_id: int, concept_ids: list[int]
    ) -> RevisionRetrievalResult:
        """读取每个概念的真实版本历史。

        「没有快照 / 没有历史」是正常情况（EMPTY），不抛 RetrievalError；
        但请求失败、HTTP 错误或响应无法解析必须记成 DEGRADED —— 否则
        「Revision 查询失败」会被模型解释成「用户过去没有误区、没有变化」。
        后端接口本身按登录用户鉴权，这里不自行推断权限。
        """
        revisions: dict[int, list[UnderstandingRevision]] = {}
        failed: list[int] = []
        for concept_id in concept_ids:
            ok, data, failure = await self._get_outcome(
                f"/api/understandings/{concept_id}/revisions"
            )
            if not ok:
                log.warning(
                    "历史轨迹查询失败：concept=%s reason=%s", concept_id, failure
                )
                failed.append(concept_id)
                revisions[concept_id] = []
                continue
            if data is None:
                # 没有快照/没有历史，正常空结果。
                revisions[concept_id] = []
                continue
            if not isinstance(data, list):
                log.warning("历史轨迹响应格式异常：concept=%s", concept_id)
                failed.append(concept_id)
                revisions[concept_id] = []
                continue
            parsed = _parse_list(data, UnderstandingRevision)
            if len(parsed) < len(data):
                # 部分版本无法解析：已解析的仍可用，但整体必须标成不完整。
                failed.append(concept_id)
            parsed.sort(key=lambda item: item.revision_no)
            revisions[concept_id] = parsed

        if failed:
            return RevisionRetrievalResult(
                revisions=revisions,
                status=RetrievalStatus.DEGRADED,
                warning=(
                    f"历史轨迹检索失败或不完整（conceptIds={failed}），"
                    "无法确认用户过去是否存在误区或变化。"
                ),
                failed_concept_ids=failed,
            )
        return RevisionRetrievalResult(
            revisions=revisions,
            status=(
                RetrievalStatus.SUCCESS
                if any(revisions.values())
                else RetrievalStatus.EMPTY
            ),
        )

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
        需要区分「请求失败」与「后端明确返回 null」的调用方应改用 `_get_outcome`。
        """
        ok, data, failure = await self._get_outcome(path, params=params)
        if ok:
            return data
        if fatal:
            raise RetrievalError(f"检索请求失败（{failure}）：{path}")
        return None

    async def _get_outcome(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> tuple[bool, Any, str | None]:
        """返回 ``(ok, data, failure)``。

        ``ok=False`` 只表示请求/统一响应体这一层失败（连接、非 2xx、非统一响应、
        业务错误），与「请求成功但 data 为 null」严格区分 —— 后者是正常空结果。
        ``failure`` 是脱敏的失败分类，仅供日志/状态诊断，不进入 Prompt。
        """
        try:
            response = await self._client.get(f"{self._base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            log.warning("检索请求失败，跳过：%s（%s）", path, exc)
            return False, None, type(exc).__name__

        if response.status_code == 401:
            raise RetrievalError(
                "后端返回 401：凭据无效（用户 JWT 过期，或 AILS_INTERNAL_TOKEN 与后端不一致）"
            )
        if response.status_code >= 400:
            log.warning("检索请求返回 %s，跳过：%s", response.status_code, path)
            return False, None, f"HTTP_{response.status_code}"

        try:
            envelope = ApiResponse.model_validate(response.json())
        except ValueError:
            log.warning("响应不是统一响应体，跳过：%s", path)
            return False, None, "INVALID_ENVELOPE"
        if not envelope.is_ok():
            log.warning("后端业务错误，跳过：%s code=%s", path, envelope.code)
            return False, None, f"BUSINESS_CODE_{envelope.code}"
        return True, envelope.data, None


def _merge_memory_results(
    results: list[MemoryRetrievalResult],
    concept_ids: list[int],
    limit: int,
) -> MemoryRetrievalResult:
    """合并多概念的 hybrid 检索结果。

    合并规则（刻意保守）：
    - 按「每个概念的第 1 条、第 2 条……」轮转交错，避免某个概念的候选被整体挤掉；
    - 按 memory.id 去重，保留首次出现；
    - 任一次检索 DEGRADED → 整体 DEGRADED（部分结果仍保留，绝不压成 EMPTY）；
    - warning / detail 去重后保留，便于区分「没结果」与「没查成」。
    """
    merged: list[MemoryItem] = []
    seen: set[int] = set()
    depth = max((len(result.items) for result in results), default=0)
    for index in range(depth):
        for result in results:
            if index >= len(result.items):
                continue
            item = result.items[index]
            if item.id in seen:
                continue
            seen.add(item.id)
            merged.append(item)

    if any(result.status is RetrievalStatus.DEGRADED for result in results):
        status = RetrievalStatus.DEGRADED
    elif merged:
        status = RetrievalStatus.SUCCESS
    else:
        status = RetrievalStatus.EMPTY

    details = [result.detail for result in results if result.detail]
    warnings = [result.warning for result in results if result.warning]
    warning = None
    if warnings:
        warning = (
            f"记忆检索结果不完整（conceptIds={concept_ids}）："
            + "；".join(dict.fromkeys(warnings))
        )
    return MemoryRetrievalResult(
        items=merged[:limit],
        status=status,
        detail=",".join(dict.fromkeys(details)) or None,
        warning=warning,
    )


def _parse_memory_search(
    data: Any,
) -> tuple[str | None, list[MemoryItem], list[str]]:
    """解析混合检索响应：``{status, items:[{memory:...}], warnings}``。

    返回 ``(status, memories, warnings)``。响应结构异常时返回空结果并记录日志，
    而不是抛出打断整轮对话；但只要后端给了 ``status``，调用方就必须据此决定
    是继续使用结果、告警降级，还是把「不可用」与「没有结果」区分开。
    """
    if data is None:
        # _get 已经为 HTTP/业务失败打过日志；这里只表示「本次没有数据」。
        return None, [], []
    if isinstance(data, list):
        # 旧后端（或反向代理缓存）仍返回裸列表：没有 status，无法判断降级状态。
        log.warning("记忆检索响应缺少 status（旧格式），无法判断是否降级")
        return None, _parse_list(data, MemoryItem), []
    if not isinstance(data, dict):
        log.warning("记忆检索响应格式异常：%s", str(data)[:200])
        return None, [], []

    raw_status = data.get("status")
    status = raw_status if isinstance(raw_status, str) else None
    if status is None:
        log.warning("记忆检索响应缺少 status，无法判断是否降级")

    memories: list[MemoryItem] = []
    raw_items = data.get("items")
    if isinstance(raw_items, list):
        for hit in raw_items:
            memory = hit.get("memory") if isinstance(hit, dict) else None
            if memory is None:
                log.warning("跳过缺少 memory 的混合检索结果：%s", str(hit)[:200])
                continue
            item = _parse_one(memory, MemoryItem)
            if item is not None:
                memories.append(item)

    raw_warnings = data.get("warnings")
    warnings = (
        [warning for warning in raw_warnings if isinstance(warning, str)]
        if isinstance(raw_warnings, list)
        else []
    )
    return status, memories, warnings


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
