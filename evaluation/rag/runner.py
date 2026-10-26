"""真实 RAG + 真实 LLM 评测执行器。

它做三件事，且只做这三件事：

1. 在**测试用户 / 测试 Space / 测试 collection** 里上传固定语料，等文档 READY，
   记录 Spring `ready-ids` 白名单；
2. 对每条用例分别构造 plain / rag / rag+state 三种上下文，走真实的
   `AgentRuntime.prepare → generate → validate_citations` 链路；
3. 把每轮的检索、最终上下文、模型原文、引用标签、四层评分写到 reports。

它**不**清理用户已有数据，**不**自动重索引，**不**改阈值。运行前由启动命令
显式提供测试数据库、测试 Qdrant prefix 与真实模型配置；缺失时直接失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from app.citations import provided_labels_from_context
from app.config import Settings
from app.embedding import build_embedding_provider
from app.llm import build_llm_adapter
from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.retriever.knowledge import KnowledgeSearchService
from app.runtime import AgentRuntime
from app.schemas import (
    ChatRequest,
    Concept,
    EvidenceItem,
    MemoryItem,
    MemoryScopeType,
    Understanding,
)
from app.vectorstore.qdrant_store import QdrantDocumentStore, collection_name_for

from .models import (
    AnswerObservation,
    ContextObservation,
    EvalCase,
    MODES,
    RetrievalObservation,
    RetrievedChunk,
    load_cases,
)
from .scoring import CaseScore, aggregate, score_case
from .scoring_v2 import aggregate_v2, score_case_v2

EVAL_ROOT = Path(__file__).resolve().parent
CORPUS_DIR = EVAL_ROOT / "corpus"
DATASET_PATH = EVAL_ROOT / "dataset.jsonl"
DATASET_V2_PATH = EVAL_ROOT / "dataset_v2.jsonl"
LEARNING_STATE_PATH = EVAL_ROOT / "learning_state.json"
REPORTS_DIR = EVAL_ROOT / "reports"


# ===================== 后端调用 =====================


class BackendError(RuntimeError):
    """后端调用失败；消息里只放状态码与业务 message，不放 token。"""


class BackendClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def register_or_login(self, username: str, password: str) -> tuple[str, int]:
        payload = {"username": username, "password": password, "email": f"{username}@example.test"}
        response = await self._client.post("/api/auth/register", json=payload)
        if response.status_code >= 400:
            response = await self._client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
        data = self._unwrap(response, "注册/登录")
        token = data.get("token")
        user = data.get("user") or {}
        user_id = user.get("id")
        if not isinstance(token, str) or not token or not isinstance(user_id, int):
            raise BackendError("注册/登录响应缺少 token 或 user.id")
        return token, user_id

    async def create_space(self, token: str, name: str, description: str) -> int:
        data = self._unwrap(
            await self._client.post(
                "/api/spaces",
                json={"name": name, "description": description, "visibility": "PRIVATE"},
                headers=_auth(token),
            ),
            "创建空间",
        )
        space_id = data.get("id")
        if not isinstance(space_id, int):
            raise BackendError("创建空间响应缺少 id")
        return space_id

    async def upload_document(self, token: str, space_id: int, path: Path) -> int:
        mime = "text/markdown" if path.suffix.lower() in {".md", ".markdown"} else "text/plain"
        response = await self._client.post(
            f"/api/spaces/{space_id}/documents",
            files={"file": (path.name, path.read_bytes(), mime)},
            headers=_auth(token),
            timeout=120.0,
        )
        data = self._unwrap(response, f"上传 {path.name}")
        document = data.get("document") or {}
        document_id = document.get("id")
        if not isinstance(document_id, int):
            raise BackendError(f"上传 {path.name} 响应缺少 document.id")
        return document_id

    async def list_documents(self, token: str, space_id: int) -> list[dict[str, Any]]:
        data = self._unwrap(
            await self._client.get(f"/api/spaces/{space_id}/documents", headers=_auth(token)),
            "列出文档",
        )
        if not isinstance(data, list):
            raise BackendError("文档列表响应不是数组")
        return [item for item in data if isinstance(item, dict)]

    async def wait_ready(
        self,
        token: str,
        space_id: int,
        expected_document_ids: list[int],
        *,
        timeout_seconds: float = 240.0,
    ) -> list[dict[str, Any]]:
        expected = set(expected_document_ids)
        deadline = time.monotonic() + timeout_seconds
        last: list[dict[str, Any]] = []
        while True:
            documents = await self.list_documents(token, space_id)
            last = [doc for doc in documents if doc.get("id") in expected]
            by_id = {doc.get("id"): doc for doc in last}
            pending = [
                doc_id
                for doc_id in expected
                if by_id.get(doc_id, {}).get("status") != "READY"
                or not by_id.get(doc_id, {}).get("chunkCount")
                or not by_id.get(doc_id, {}).get("indexedAt")
            ]
            failed = [doc for doc in last if doc.get("status") == "FAILED"]
            if failed:
                names = ", ".join(str(doc.get("name")) for doc in failed)
                errors = "; ".join(str(doc.get("indexError")) for doc in failed)
                raise BackendError(f"文档索引失败：{names}；{errors}")
            if not pending:
                return last
            if time.monotonic() >= deadline:
                statuses = ", ".join(
                    f"{doc.get('name')}={doc.get('status')}/{doc.get('chunkCount')}"
                    for doc in last
                )
                raise BackendError(f"等待文档 READY 超时（{timeout_seconds:.0f}s）：{statuses}")
            await asyncio.sleep(2.0)

    async def ready_ids(self, token: str, space_id: int, embedding_model: str) -> list[int]:
        response = await self._client.get(
            f"/api/spaces/{space_id}/documents/ready-ids",
            params={"embeddingModel": embedding_model},
            headers=_auth(token),
        )
        data = self._unwrap(response, "ready-ids")
        if not isinstance(data, list) or any(
            isinstance(item, bool) or not isinstance(item, int) for item in data
        ):
            raise BackendError("ready-ids 响应不是合法整数数组")
        return data

    async def create_conversation(self, token: str, space_id: int, title: str) -> int:
        data = self._unwrap(
            await self._client.post(
                f"/api/spaces/{space_id}/conversations",
                json={"title": title},
                headers=_auth(token),
            ),
            "创建对话",
        )
        conversation_id = data.get("id")
        if not isinstance(conversation_id, int):
            raise BackendError("创建对话响应缺少 id")
        return conversation_id

    async def stream_message(
        self, token: str, conversation_id: int, content: str, *, timeout_seconds: float = 300.0
    ) -> list[tuple[str, dict[str, Any]]]:
        """发一条消息并完整收集 Spring SSE 事件。"""
        events: list[tuple[str, dict[str, Any]]] = []
        async with self._client.stream(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            json={"content": content},
            headers={**_auth(token), "Accept": "text/event-stream"},
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise BackendError(f"发送消息 HTTP {response.status_code}：{body[:500]}")
            event_name: str | None = None
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
                elif not line.strip():
                    if event_name:
                        events.append((event_name, _json_payload(data_lines)))
                    event_name = None
                    data_lines = []
            if event_name:
                events.append((event_name, _json_payload(data_lines)))
        return events

    def _unwrap(self, response: httpx.Response, action: str) -> Any:
        if response.status_code >= 400:
            raise BackendError(f"{action} HTTP {response.status_code}：{response.text[:500]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise BackendError(f"{action} 响应不是 JSON") from exc
        if not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("message") if isinstance(body, dict) else body
            raise BackendError(f"{action} 业务失败：{message}")
        return body.get("data")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _json_payload(lines: list[str]) -> dict[str, Any]:
    raw = "\n".join(lines).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"raw": value}


# ===================== 受控检索源 =====================


class EvalRetriever:
    """把受控学习状态 + 真实知识检索拼成一个 Retriever。

    - `concept_mode=none`：不返回 Concept，Runtime 不会检索 U/M/E（plain / rag）；
    - `concept_mode=controlled`：返回 `learning_state.json` 里的受控学习状态（rag+state）；
    - `knowledge_mode=off`：plain 模式不检索文档；
    - 其余情况调用真实的 `KnowledgeSearchService`（白名单由 Spring ready-ids 提供）。
    """

    def __init__(
        self,
        *,
        name: str,
        concept_mode: str,
        knowledge_mode: str,
        knowledge_search: KnowledgeSearchService | None,
        allowed_document_ids: list[int],
        empty_space: bool = False,
        concepts: list[Concept] | None = None,
        understandings: list[Understanding] | None = None,
        memories: list[MemoryItem] | None = None,
        evidences: list[EvidenceItem] | None = None,
    ) -> None:
        self._name = name
        self._concept_mode = concept_mode
        self._knowledge_mode = knowledge_mode
        self._knowledge_search = knowledge_search
        self._allowed_document_ids = list(allowed_document_ids)
        self._empty_space = empty_space
        self._concepts = list(concepts or [])
        self._understandings = list(understandings or [])
        self._memories = list(memories or [])
        self._evidences = list(evidences or [])

    @property
    def name(self) -> str:
        return self._name

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return list(self._concepts) if self._concept_mode == "controlled" else []

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        wanted = set(concept_ids)
        return [item for item in self._understandings if item.concept_id in wanted]

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        wanted = set(concept_ids)
        result: list[MemoryItem] = []
        for item in self._memories:
            if item.scope_type == MemoryScopeType.GLOBAL:
                result.append(item)
            elif item.concept_id in wanted:
                result.append(item)
            elif item.scope_type == MemoryScopeType.SPACE and item.concept_id is None:
                result.append(item)
        return result[:5]

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        wanted = set(concept_ids)
        return [item for item in self._evidences if item.concept_id in wanted][:limit]

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        if self._knowledge_mode == "off":
            return KnowledgeResult(
                status=KnowledgeStatus.NOT_RETRIEVED,
                warning="评测 plain 模式不检索文档。",
            )
        if self._empty_space:
            return KnowledgeResult(
                status=KnowledgeStatus.NO_VALID_DOCUMENTS,
                warning="本空间没有已就绪且索引完整的文档，本次回答未使用上传资料。",
            )
        if self._knowledge_search is None:
            return KnowledgeResult.degraded("知识检索组件未配置，本次回答未使用文档资料。")
        try:
            return await self._knowledge_search.search(query, self._allowed_document_ids)
        except Exception as exc:  # noqa: BLE001 —— 与生产边界一致：检索故障降级
            return KnowledgeResult.degraded(
                f"知识检索依赖不可用，本次回答未使用文档资料。（{type(exc).__name__}）"
            )

    async def aclose(self) -> None:
        # 共享 provider/store 由 runner 统一关闭，避免每个 case 关闭连接池。
        return None


class BrokenEmbeddingProvider:
    """模拟 Embedding 依赖不可用；只用于 F04 降级路径。"""

    def __init__(self, *, model: str, dimension: int) -> None:
        self.model = model
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("evaluation: embedding dependency unavailable")

    async def aclose(self) -> None:
        return None


# ===================== 环境与运行 =====================


@dataclass
class LiveEnvironment:
    settings: Settings
    backend: BackendClient
    token: str
    user_id: int
    main_space_id: int
    empty_space_id: int
    documents: list[dict[str, Any]]
    ready_ids: list[int]
    collection: str
    embedding_provider: Any
    qdrant_store: QdrantDocumentStore
    knowledge_search: KnowledgeSearchService
    bad_qdrant_store: QdrantDocumentStore | None = None
    bad_qdrant_search: KnowledgeSearchService | None = None
    broken_provider: BrokenEmbeddingProvider | None = None
    broken_search: KnowledgeSearchService | None = None

    async def aclose(self) -> None:
        await self.backend.aclose()
        if self.bad_qdrant_store is not None:
            await self.bad_qdrant_store.aclose()
        await self.qdrant_store.aclose()
        await self.embedding_provider.aclose()


async def prepare_environment(backend_url: str, *, upload: bool = True) -> LiveEnvironment:
    settings = Settings()
    guard_live_settings(settings)

    backend = BackendClient(backend_url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    username = f"phase42_eval_{stamp}"
    password = "Phase42-Eval-2026!"
    token, user_id = await backend.register_or_login(username, password)

    main_space_id = await backend.create_space(
        token, f"Phase 4.2 RAG Eval {stamp}", "RAG 评测专用；请勿用于真实业务"
    )
    empty_space_id = await backend.create_space(
        token, f"Phase 4.2 RAG Empty {stamp}", "无文档空间；用于降级验收"
    )

    documents: list[dict[str, Any]] = []
    if upload:
        corpus_files = sorted(CORPUS_DIR.glob("*.md"))
        if not corpus_files:
            raise RuntimeError(f"评测语料为空：{CORPUS_DIR}")
        document_ids: list[int] = []
        for path in corpus_files:
            document_id = await backend.upload_document(token, main_space_id, path)
            document_ids.append(document_id)
            # 串行等待：并行建索引会在 MySQL 里同时写 document_chunk，
            # 测试库上出现过死锁并把文档置 FAILED。验收脚本不该靠重试掩盖它，
            # 但也不该主动制造并发；逐篇 READY 后再传下一篇。
            documents.extend(
                await backend.wait_ready(token, main_space_id, [document_id])
            )
        documents = await backend.wait_ready(token, main_space_id, document_ids)
        ready_ids = await backend.ready_ids(token, main_space_id, settings.embedding_model)
        if set(ready_ids) != set(document_ids):
            raise RuntimeError(
                f"ready-ids 与上传文档不一致：uploaded={document_ids} ready={ready_ids}"
            )
    else:
        ready_ids = await backend.ready_ids(token, main_space_id, settings.embedding_model)

    embedding_provider = build_embedding_provider(settings)
    qdrant_store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    collection = collection_name_for(
        settings.qdrant_collection_prefix,
        embedding_provider.model,
        embedding_provider.dimension,
    )
    knowledge_search = KnowledgeSearchService(
        provider=embedding_provider,
        store=qdrant_store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )

    bad_qdrant_store = QdrantDocumentStore(
        url="http://127.0.0.1:1",
        api_key="",
        timeout_seconds=3.0,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    bad_qdrant_search = KnowledgeSearchService(
        provider=embedding_provider,
        store=bad_qdrant_store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=5.0,
    )
    broken_provider = BrokenEmbeddingProvider(
        model=settings.embedding_model, dimension=settings.embedding_dimension
    )
    broken_search = KnowledgeSearchService(
        provider=broken_provider,
        store=qdrant_store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=5.0,
    )

    return LiveEnvironment(
        settings=settings,
        backend=backend,
        token=token,
        user_id=user_id,
        main_space_id=main_space_id,
        empty_space_id=empty_space_id,
        documents=documents,
        ready_ids=ready_ids,
        collection=collection,
        embedding_provider=embedding_provider,
        qdrant_store=qdrant_store,
        knowledge_search=knowledge_search,
        bad_qdrant_store=bad_qdrant_store,
        bad_qdrant_search=bad_qdrant_search,
        broken_provider=broken_provider,
        broken_search=broken_search,
    )


def guard_live_settings(settings: Settings) -> None:
    """真实评测必须显式配置；缺 Key 直接失败，绝不静默退回 Mock。"""
    if settings.llm_provider.strip().lower() != "openai":
        raise RuntimeError(
            "真实 LLM 未启用：请设置 AILS_LLM_PROVIDER=openai（不会自动退回 Mock）"
        )
    if not settings.llm_api_key:
        raise RuntimeError("AILS_LLM_PROVIDER=openai 但没有 AILS_LLM_API_KEY；拒绝以 Mock 运行真实评测")
    if settings.embedding_provider.strip().lower() not in {"openai", "openai-compatible"}:
        raise RuntimeError("真实评测要求 AILS_EMBEDDING_PROVIDER=openai")
    if not settings.embedding_api_key:
        raise RuntimeError("真实评测要求 AILS_EMBEDDING_API_KEY")
    if not settings.qdrant_collection_prefix.startswith("ails_phase42"):
        raise RuntimeError(
            "真实评测必须使用本轮专用 Qdrant prefix（ails_phase42...），"
            f"当前为 {settings.qdrant_collection_prefix!r}"
        )


def load_controlled_state(path: Path) -> dict[str, list[Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "concepts": [Concept.model_validate(item) for item in raw.get("concepts", [])],
        "understandings": [
            Understanding.model_validate(item) for item in raw.get("understandings", [])
        ],
        "memories": [MemoryItem.model_validate(item) for item in raw.get("memories", [])],
        "evidences": [EvidenceItem.model_validate(item) for item in raw.get("evidences", [])],
    }


# ===================== 单轮执行 =====================


def make_retriever(
    env: LiveEnvironment,
    case: EvalCase,
    mode: str,
    controlled: dict[str, list[Any]],
) -> tuple[EvalRetriever, int]:
    knowledge_mode = "off" if mode == "plain" else "on"
    concept_mode = "controlled" if mode == "rag+state" else "none"
    search = env.knowledge_search
    allowed = list(env.ready_ids)
    space_id = env.main_space_id
    empty_space = False
    name = f"eval:{mode}:{case.scenario}"

    if case.scenario == "empty_space":
        allowed = []
        space_id = env.empty_space_id
        empty_space = True
    elif case.scenario == "qdrant_down":
        search = env.bad_qdrant_search
    elif case.scenario == "embedding_down":
        search = env.broken_search

    return EvalRetriever(
        name=name,
        concept_mode=concept_mode,
        knowledge_mode=knowledge_mode,
        knowledge_search=search,
        allowed_document_ids=allowed,
        empty_space=empty_space,
        concepts=controlled["concepts"],
        understandings=controlled["understandings"],
        memories=controlled["memories"],
        evidences=controlled["evidences"],
    ), space_id


def observe_retrieval(
    env: LiveEnvironment, prepared, scenario: str
) -> RetrievalObservation:
    state = prepared.state
    chunks = [
        RetrievedChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id or 0,
            document_name=chunk.document_name or "",
            chunk_index=chunk.chunk_index if chunk.chunk_index is not None else -1,
            score=float(chunk.score or 0.0),
            content=chunk.content,
            heading_path=tuple(chunk.heading_path or ()),
        )
        for chunk in state.knowledge
    ]
    candidates = 0
    elapsed = 0
    for event in prepared.trace.events:
        if event.type.value == "RETRIEVE":
            candidates = int(event.detail.get("knowledgeCandidates") or 0)
            elapsed = event.elapsed_ms
            break
    allowlist = 0 if scenario == "empty_space" else len(env.ready_ids)
    return RetrievalObservation(
        status=str(state.knowledge_status),
        warning=state.knowledge_warning,
        chunks=chunks,
        candidates=candidates,
        duration_ms=elapsed,
        collection=env.collection,
        allowlist_size=allowlist,
    )


def observe_context(prepared, mode: str) -> ContextObservation:
    context = prepared.context
    by_id = {str(chunk.id): chunk for chunk in prepared.state.knowledge}
    documents: list[str] = []
    chunk_ids: list[int] = []
    for section in context.sections:
        if not str(section.name).startswith("knowledge:") or section.dropped:
            continue
        chunk = by_id.get(str(section.name).split(":", 1)[1])
        if chunk is None or chunk.document_name is None or chunk.chunk_index is None:
            continue
        if chunk.document_name not in documents:
            documents.append(chunk.document_name)
        chunk_ids.append(chunk.id)
    labels = provided_labels_from_context(context, prepared.state.knowledge)
    dropped = sum(
        1
        for section in context.sections
        if str(section.name).startswith("knowledge:") and section.dropped
    )
    text = "\n\n".join(f"[{message.role}] {message.content}" for message in context.messages)
    return ContextObservation(
        mode=mode,
        text=text,
        provided_labels=labels,
        context_documents=documents,
        context_chunk_ids=chunk_ids,
        dropped_knowledge=dropped,
        estimated_tokens=context.estimated_tokens,
        detected_concepts=prepared.state.concept_names,
        warnings=list(context.warnings),
        sections=[section.as_dict() for section in context.sections],
    )


def _score(
    case: EvalCase,
    mode: str,
    *,
    retrieval: RetrievalObservation,
    context: ContextObservation,
    answer: AnswerObservation,
    citation_report,
    rules_version: str,
) -> CaseScore:
    score_fn = score_case_v2 if rules_version == "v2" else score_case
    return score_fn(
        case,
        mode,
        retrieval=retrieval,
        context=context,
        answer=answer,
        citation_report=citation_report,
    )


async def run_one(
    env: LiveEnvironment,
    runtime: AgentRuntime,
    case: EvalCase,
    mode: str,
    controlled: dict[str, list[Any]],
    run_id: int,
    *,
    rules_version: str = "v1",
) -> dict[str, Any]:
    retriever, space_id = make_retriever(env, case, mode, controlled)
    request = ChatRequest(
        run_id=run_id,
        trace_id=f"phase42-{case.id}-{mode}",
        user_id=env.user_id,
        conversation_id=run_id,
        learning_space_id=space_id,
        user_message=case.question,
        auth_token=env.token,
    )
    prepared = None
    answer = AnswerObservation(
        provider=env.settings.llm_provider,
        model=env.settings.llm_model,
        temperature=env.settings.llm_temperature,
    )
    try:
        prepared = await runtime.prepare(request, retriever=retriever)
        started = time.perf_counter()
        parts: list[str] = []
        async for delta in runtime.generate(prepared):
            parts.append(delta)
        answer.text = "".join(parts)
        answer.latency_ms = int((time.perf_counter() - started) * 1000)
        citation_report = runtime.validate_citations(prepared)
    except Exception as exc:  # noqa: BLE001 —— 评估要记录失败，而不是整批中断
        answer.error = f"{type(exc).__name__}: {exc}"
        answer.latency_ms = 0
        from app.citations import CitationReport

        citation_report = CitationReport()
        if prepared is None:
            # prepare 失败：返回一条可解释的空观测，评分层会判负。
            retrieval = RetrievalObservation(
                status="DEGRADED",
                warning=answer.error,
                error=answer.error,
                collection=env.collection,
                allowlist_size=len(env.ready_ids),
            )
            context = ContextObservation(mode=mode)
        else:
            retrieval = observe_retrieval(env, prepared, case.scenario)
            context = observe_context(prepared, mode)
    else:
        retrieval = observe_retrieval(env, prepared, case.scenario)
        context = observe_context(prepared, mode)
    finally:
        await retriever.aclose()

    score: CaseScore = _score(
        case,
        mode,
        retrieval=retrieval,
        context=context,
        answer=answer,
        citation_report=citation_report,
        rules_version=rules_version,
    )
    record: dict[str, Any] = {
        "case": case.as_dict(),
        "mode": mode,
        "runId": run_id,
        "retrieval": retrieval.as_dict(),
        "context": context.as_dict(),
        "answer": answer.as_dict(),
        "citation": citation_report.as_dict(),
        "score": score.as_dict(),
    }
    if prepared is not None:
        record["trace"] = prepared.trace.as_dict()
    return record


# ===================== 报告 =====================


def write_reports(
    output_dir: Path,
    records: list[dict[str, Any]],
    scores: list[CaseScore],
    env: LiveEnvironment,
    *,
    modes: list[str],
    started_at: str,
    rules_version: str = "v1",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "runs.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = aggregate_v2(scores) if rules_version == "v2" else aggregate(scores)
    metadata = {
        "startedAt": started_at,
        "backendUrl": env.backend.base_url,
        "userId": env.user_id,
        "mainSpaceId": env.main_space_id,
        "emptySpaceId": env.empty_space_id,
        "documents": [
            {
                "id": doc.get("id"),
                "name": doc.get("name"),
                "status": doc.get("status"),
                "chunkCount": doc.get("chunkCount"),
                "embeddingModel": doc.get("embeddingModel"),
                "indexVersion": doc.get("indexVersion"),
            }
            for doc in env.documents
        ],
        "readyIds": env.ready_ids,
        "collection": env.collection,
        "llm": {
            "provider": env.settings.llm_provider,
            "model": env.settings.llm_model,
            "temperature": env.settings.llm_temperature,
            "maxTokens": env.settings.llm_max_tokens,
        },
        "embedding": {
            "provider": env.settings.embedding_provider,
            "model": env.settings.embedding_model,
            "dimension": env.settings.embedding_dimension,
        },
        "retrievalBaseline": {
            "topK": env.settings.knowledge_top_k,
            "minScore": env.settings.knowledge_min_score,
            "maxContextTokens": env.settings.max_context_tokens,
        },
        "modes": modes,
        "cases": len({record["case"]["id"] for record in records}),
        "rulesVersion": rules_version,
    }
    (output_dir / "summary.json").write_text(
        json.dumps({"metadata": metadata, "aggregate": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown = (
        _markdown_summary_v2(metadata, summary, records, scores)
        if rules_version == "v2"
        else _markdown_summary(metadata, summary, records, scores)
    )
    (output_dir / "summary.md").write_text(markdown, encoding="utf-8")


def _markdown_summary(
    metadata: dict[str, Any],
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    scores: list[CaseScore],
) -> str:
    lines = [
        "# Phase 4.2 RAG 评测结果",
        "",
        f"- 开始时间：{metadata['startedAt']}",
        f"- 后端：`{metadata['backendUrl']}`",
        f"- 用户：`{metadata['userId']}`，主空间：`{metadata['mainSpaceId']}`，"
        f"空空间：`{metadata['emptySpaceId']}`",
        f"- LLM：`{metadata['llm']['provider']}` / `{metadata['llm']['model']}`"
        f"（temperature={metadata['llm']['temperature']}）",
        f"- Embedding：`{metadata['embedding']['model']}` / {metadata['embedding']['dimension']} 维",
        f"- Qdrant collection：`{metadata['collection']}`",
        f"- 检索基线：top_k={metadata['retrievalBaseline']['topK']}，"
        f"min_score={metadata['retrievalBaseline']['minScore']}，"
        f"max_context_tokens={metadata['retrievalBaseline']['maxContextTokens']}",
        f"- 文档：{len(metadata['documents'])} 个，ready ids={metadata['readyIds']}",
        "",
        "## 按模式汇总",
        "",
        "| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 非法引用率 | 来源支持不足率 | 平均延迟(ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in metadata["modes"]:
        data = summary["modes"].get(mode, {})
        lines.append(
            "| {mode} | {runs} | {autoPass} | {retrieval} | {context} | {answer} | {citation} | {illegal} | {unsupported} | {latency} |".format(
                mode=mode,
                runs=data.get("runs", 0),
                autoPass=data.get("autoPass", 0),
                retrieval=_pct(data.get("retrievalPassRate")),
                context=_pct(data.get("contextPassRate")),
                answer=_pct(data.get("answerPassRate")),
                citation=_pct(data.get("citationPassRate")),
                illegal=_pct(data.get("illegalCitationRate")),
                unsupported=_pct(data.get("unsupportedCitationRate")),
                latency=data.get("averageLatencyMs"),
            )
        )

    lines.extend(
        [
            "",
            "> 检索/上下文/答案/引用是四层独立指标；引用通过只表示标签合法且必需来源被引用，"
            "“来源支持不足率”是词面启发式信号，不是事实正确性证明。",
            "",
            "## 按类别 × 模式",
            "",
            "| 模式 | 类别 | 运行 | 自动通过 | 答案通过 | 引用通过 | 非法引用率 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for mode in metadata["modes"]:
        for category, data in sorted(summary["byModeCategory"].get(mode, {}).items()):
            lines.append(
                "| {mode} | {category} | {runs} | {auto} | {answer} | {citation} | {illegal} |".format(
                    mode=mode,
                    category=category,
                    runs=data["runs"],
                    auto=data["autoPass"],
                    answer=_pct(data.get("answerPassRate")),
                    citation=_pct(data.get("citationPassRate")),
                    illegal=_pct(data.get("illegalCitationRate")),
                )
            )

    manual = [score for score in scores if score.manual_review]
    if manual:
        lines.extend(["", "## 需人工复核", ""])
        for score in manual:
            lines.append(
                f"- `{score.case_id}` / `{score.mode}`：自动评分={'通过' if score.auto_pass else '未通过'}。{score.notes}"
            )

    failures = [score for score in scores if not score.auto_pass and not score.manual_review]
    if failures:
        lines.extend(["", "## 自动评分未通过", ""])
        for score in failures:
            failed_layers = [
                name
                for name, layer in (
                    ("retrieval", score.retrieval),
                    ("context", score.context),
                    ("answer", score.answer),
                    ("citation", score.citation),
                )
                if layer.applicable and layer.passed is False
            ]
            failed_checks = [
                check.name
                for check in score.checks
                if check.passed is False
            ]
            lines.append(
                f"- `{score.case_id}` / `{score.mode}`：层={failed_layers}；检查={failed_checks}"
            )

    lines.extend(
        [
            "",
            "## 原始记录",
            "",
            "- `runs.jsonl`：每条用例 × 模式一行，含检索状态、最终上下文、模型原文、"
            "引用校验逐条结果、四层评分与 trace。",
            "- `summary.json`：机器可读汇总。",
            "",
        ]
    )
    return "\n".join(lines)


def _markdown_summary_v2(
    metadata: dict[str, Any],
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    scores: list[CaseScore],
) -> str:
    """v2 报告：引用指标拆开展示，并逐项写出分子/分母。"""
    lines = [
        "# Phase 4.3 RAG 评测结果（v2 规则）",
        "",
        f"- 开始时间：{metadata['startedAt']}",
        f"- 后端：`{metadata['backendUrl']}`",
        f"- 用户：`{metadata['userId']}`，主空间：`{metadata['mainSpaceId']}`，"
        f"空空间：`{metadata['emptySpaceId']}`",
        f"- LLM：`{metadata['llm']['provider']}` / `{metadata['llm']['model']}`"
        f"（temperature={metadata['llm']['temperature']}）",
        f"- Embedding：`{metadata['embedding']['model']}` / {metadata['embedding']['dimension']} 维",
        f"- Qdrant collection：`{metadata['collection']}`",
        f"- 检索基线：top_k={metadata['retrievalBaseline']['topK']}，"
        f"min_score={metadata['retrievalBaseline']['minScore']}，"
        f"max_context_tokens={metadata['retrievalBaseline']['maxContextTokens']}",
        f"- 文档：{len(metadata['documents'])} 个，ready ids={metadata['readyIds']}",
        "- 规则版本：v2（v1 数据集与 4.2 报告保留不变）",
        "",
        "## 按模式汇总",
        "",
        "| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 平均延迟(ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in metadata["modes"]:
        data = summary["modes"].get(mode, {})
        lines.append(
            "| {mode} | {runs} | {autoPass} | {retrieval} | {context} | {answer} | {citation} | {latency} |".format(
                mode=mode,
                runs=data.get("runs", 0),
                autoPass=data.get("autoPass", 0),
                retrieval=_pct(data.get("retrievalPassRate")),
                context=_pct(data.get("contextPassRate")),
                answer=_pct(data.get("answerPassRate")),
                citation=_pct(data.get("citationPassRate")),
                latency=data.get("averageLatencyMs"),
            )
        )

    lines.extend(
        [
            "",
            "> `citation_pass_rate` 的分母是运行数；下面六个指标的分母各不相同，"
            "必须按各自的 numerator/denominator 阅读，不能横向比较百分比。",
            "> 来源支持度是词面启发式线索，只用于人工复核，不是事实准确率。",
            "",
            "## 引用质量指标（分子 / 分母）",
            "",
            "| 模式 | 非法标签率 | 格式错误率 | 缺必需来源率 | 禁止来源率 | 额外合法来源率 | 支持警告率 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    order = [
        "illegal_label_rate",
        "malformed_label_rate",
        "required_source_missing_rate",
        "forbidden_source_rate",
        "extra_valid_source_rate",
        "citation_support_warning_rate",
    ]
    for mode in metadata["modes"]:
        metrics = summary["modes"].get(mode, {}).get("metrics", {})
        cells = [_metric_cell(metrics.get(name)) for name in order]
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")

    lines.extend(
        [
            "",
            "## 指标定义",
            "",
        ]
    )
    for name in order:
        definition = None
        for mode in metadata["modes"]:
            metric = summary["modes"].get(mode, {}).get("metrics", {}).get(name)
            if metric:
                definition = metric["definition"]
                break
        lines.append(f"- `{name}`：{definition or 'N/A'}。")

    lines.extend(
        [
            "",
            "## 按类别 × 模式",
            "",
            "| 模式 | 类别 | 运行 | 自动通过 | 答案通过 | 引用通过 |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for mode in metadata["modes"]:
        for category, data in sorted(summary["byModeCategory"].get(mode, {}).items()):
            lines.append(
                "| {mode} | {category} | {runs} | {auto} | {answer} | {citation} |".format(
                    mode=mode,
                    category=category,
                    runs=data["runs"],
                    auto=data["autoPass"],
                    answer=_pct(data.get("answerPassRate")),
                    citation=_pct(data.get("citationPassRate")),
                )
            )

    manual = [score for score in scores if score.manual_review]
    if manual:
        lines.extend(["", "## 需人工复核", ""])
        for score in manual:
            lines.append(
                f"- `{score.case_id}` / `{score.mode}`：自动评分={'通过' if score.auto_pass else '未通过'}。{score.notes}"
            )

    failures = [score for score in scores if not score.auto_pass and not score.manual_review]
    if failures:
        lines.extend(["", "## 自动评分未通过", ""])
        for score in failures:
            failed_layers = [
                name
                for name, layer in (
                    ("retrieval", score.retrieval),
                    ("context", score.context),
                    ("answer", score.answer),
                    ("citation", score.citation),
                )
                if layer.applicable and layer.passed is False
            ]
            failed_checks = [
                check.name for check in score.checks if check.passed is False
            ]
            lines.append(
                f"- `{score.case_id}` / `{score.mode}`：层={failed_layers}；检查={failed_checks}"
            )

    lines.extend(
        [
            "",
            "## 原始记录",
            "",
            "- `runs.jsonl`：每条用例 × 模式一行，含检索状态、最终上下文、模型原文、"
            "引用校验逐条结果、v2 四层评分与 trace。",
            "- `summary.json`：机器可读汇总，含每个指标的 numerator / denominator。",
            "",
        ]
    )
    return "\n".join(lines)


def _metric_cell(metric: dict[str, Any] | None) -> str:
    if not metric:
        return "N/A"
    rate = metric.get("rate")
    rendered = "N/A" if rate is None else f"{float(rate) * 100:.1f}%"
    return f"{rendered} ({metric['numerator']}/{metric['denominator']})"


def _pct(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.1f}%"


# ===================== 入口 =====================


async def run_eval(
    *,
    backend_url: str,
    modes: list[str],
    categories: list[str] | None,
    max_cases: int | None,
    output_dir: Path | None,
    rules_version: str = "v1",
    case_ids: list[str] | None = None,
) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    dataset = DATASET_V2_PATH if rules_version == "v2" else DATASET_PATH
    cases = load_cases(dataset)
    if case_ids:
        wanted = {item.upper() for item in case_ids}
        cases = [case for case in cases if case.id.upper() in wanted]
    if categories:
        cases = [case for case in cases if case.category in set(categories)]
    if max_cases:
        cases = cases[:max_cases]
    if not cases:
        raise RuntimeError("筛选后没有用例")

    env = await prepare_environment(backend_url)
    controlled = load_controlled_state(LEARNING_STATE_PATH)
    llm = build_llm_adapter(env.settings)
    runtime = AgentRuntime(env.settings, llm, knowledge_search=env.knowledge_search)
    records: list[dict[str, Any]] = []
    scores: list[CaseScore] = []
    run_id = 420000
    try:
        for case in cases:
            for mode in modes:
                if not case.applies_to(mode):
                    continue
                record = await run_one(
                    env, runtime, case, mode, controlled, run_id,
                    rules_version=rules_version,
                )
                run_id += 1
                records.append(record)
                # run_one 已经算出 score，这里用原始观测重建，避免跨模块私有状态。
                score = _record_to_score(record, case, rules_version=rules_version)
                scores.append(score)
                answer = record["answer"]
                citation = record["citation"]
                print(
                    f"[{case.id}/{mode}] {score.status} "
                    f"answer={score.answer.passed} citation={score.citation.passed} "
                    f"invalid={citation.get('invalid')}/{citation.get('total')} "
                    f"latency={answer.get('latencyMs')}ms",
                    flush=True,
                )
                if answer.get("error"):
                    print(f"  error: {answer['error']}", flush=True)
    finally:
        await runtime.aclose()
        await env.aclose()

    if output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_dir = REPORTS_DIR / f"real_{stamp}_{rules_version}"
    write_reports(
        output_dir, records, scores, env,
        modes=modes, started_at=started_at, rules_version=rules_version,
    )
    summary = aggregate_v2(scores) if rules_version == "v2" else aggregate(scores)
    print(json.dumps({"output": str(output_dir), "aggregate": summary["modes"]}, ensure_ascii=False, indent=2))
    return 0


def _record_to_score(
    record: dict[str, Any], case: EvalCase, *, rules_version: str = "v1"
) -> CaseScore:
    """从已写入报告的记录还原 CaseScore，避免在 run_one 外面重复评分。"""
    from .models import AnswerObservation, ContextObservation, RetrievalObservation, RetrievedChunk

    retrieval_data = record["retrieval"]
    chunks = [
        RetrievedChunk(
            chunk_id=item["chunkId"],
            document_id=item["documentId"],
            document_name=item["documentName"],
            chunk_index=item["chunkIndex"],
            score=item["score"],
            content=item.get("content", ""),
            heading_path=tuple(item.get("headingPath", [])),
        )
        for item in retrieval_data.get("chunks", [])
    ]
    retrieval = RetrievalObservation(
        status=retrieval_data["status"],
        warning=retrieval_data.get("warning"),
        chunks=chunks,
        candidates=retrieval_data.get("candidates"),
        duration_ms=retrieval_data.get("durationMs", 0),
        collection=retrieval_data.get("collection"),
        error=retrieval_data.get("error"),
        allowlist_size=retrieval_data.get("allowlistSize"),
    )
    context_data = record["context"]
    context = ContextObservation(
        mode=context_data["mode"],
        text=context_data.get("text", ""),
        provided_labels=context_data.get("providedLabels", []),
        context_documents=context_data.get("contextDocuments", []),
        context_chunk_ids=context_data.get("contextChunkIds", []),
        dropped_knowledge=context_data.get("droppedKnowledge", 0),
        estimated_tokens=context_data.get("estimatedTokens", 0),
        detected_concepts=context_data.get("detectedConcepts", []),
        warnings=context_data.get("warnings", []),
        sections=context_data.get("sections", []),
    )
    answer_data = record["answer"]
    answer = AnswerObservation(
        text=answer_data.get("text", ""),
        latency_ms=answer_data.get("latencyMs", 0),
        error=answer_data.get("error"),
        provider=answer_data.get("provider", ""),
        model=answer_data.get("model", ""),
        temperature=answer_data.get("temperature"),
    )
    from app.citations import Citation, CitationReport

    citation_data = record["citation"]
    citations = [
        Citation(
            raw=item["raw"],
            document_name=item["documentName"],
            chunk_index=item["chunkIndex"],
            valid=item["valid"],
            chunk_id=item.get("chunkId"),
            claim=item.get("claim", ""),
            source_excerpt=item.get("sourceExcerpt"),
            lexical_support=item.get("lexicalSupport", "none"),
            overlap_terms=tuple(item.get("overlapTerms", [])),
            numeric_conflict=bool(item.get("numericConflict", False)),
        )
        for item in citation_data.get("citations", [])
    ]
    report = CitationReport(
        provided_labels=citation_data.get("providedLabels", []),
        citations=citations,
        invalid_labels=citation_data.get("invalidLabels", []),
        malformed_labels=citation_data.get("malformedLabels", []),
        duplicate_labels=citation_data.get("duplicateLabels", []),
        cited_documents=citation_data.get("citedDocuments", []),
    )
    return _score(
        case,
        record["mode"],
        retrieval=retrieval,
        context=context,
        answer=answer,
        citation_report=report,
        rules_version=rules_version,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 4.2 RAG + real LLM evaluation")
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:8080",
        help="Spring Boot base URL（必须指向测试数据库实例）",
    )
    parser.add_argument(
        "--modes",
        default="plain,rag,rag+state",
        help="逗号分隔：plain,rag,rag+state",
    )
    parser.add_argument("--categories", default="", help="逗号分隔 A,B,C,D,E,F；空=全部")
    parser.add_argument("--cases", default="", help="逗号分隔用例 id，如 D01,D04；空=全部")
    parser.add_argument("--max-cases", type=int, default=0, help="只跑前 N 条用例（调试用）")
    parser.add_argument("--output-dir", default="", help="报告目录；空=自动生成")
    parser.add_argument(
        "--rules-version",
        choices=("v1", "v2"),
        default="v1",
        help="v1=4.2 原规则；v2=4.3 规则（独立数据集与报告）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        raise SystemExit(f"未知 mode: {unknown}；可选 {list(MODES)}")
    categories = [item.strip().upper() for item in args.categories.split(",") if item.strip()]
    if categories and (set(categories) - {"A", "B", "C", "D", "E", "F"}):
        raise SystemExit(f"未知 category: {categories}")
    case_ids = [item.strip() for item in args.cases.split(",") if item.strip()]
    try:
        return asyncio.run(
            run_eval(
                backend_url=args.backend_url,
                modes=modes,
                categories=categories or None,
                max_cases=args.max_cases or None,
                output_dir=Path(args.output_dir) if args.output_dir else None,
                rules_version=args.rules_version,
                case_ids=case_ids or None,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 —— CLI 入口给人看可读错误
        print(f"评测失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
