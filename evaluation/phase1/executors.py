"""Phase 1 用例执行器。

所有用例都复用 Agent 的真实运行时（``AgentRuntime`` / ``ContextBuilder`` /
``HistoryGuard`` / ``EvidenceGuard`` / ``LearningAnalyzer``），只替换两个边界：

- Retriever：换成按 userId/spaceId 过滤的 ``EvalRetriever``；
- LLM：换成记录型包装，内部可以是 mock、固定输出或真实模型。

没有复制一份业务逻辑。失败因此可以归因到真实代码路径，而不是评测脚本。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evaluation.runner.scripted_llm import ScriptedLLMAdapter

from app.config import AGENT_ROOT, Settings
from app.evidence_guard import already_discloses, status_notice
from app.llm.base import LLMError
from app.llm.factory import build_llm_adapter
from app.llm.mock import MockLLMAdapter
from app.retriever import build_retriever
from app.retriever.base import RetrievalError
from app.runtime import AgentRuntime
from app.schemas import AnalyzeRequest, ChatRequest, HistoryMessage, SessionContext

from .claims import analyze_mastery_claim
from .fixtures import make_retriever
from .models import EvalCase
from .recording import RecordingLLMAdapter, SequenceLLMAdapter

AGENT_ENV = AGENT_ROOT / ".env"
DEFAULT_TIMEOUT = 45.0

_INTERNAL_ID_RE = re.compile(
    r"(concept_id|chunk_id|memory:|evidence:|revision:|understanding:)"
    r"|\b(?:9201|9202|9203|9204|9301|1201|1221|1301|1302|1303|1312|1313|1321|"
    r"1331|1401|1402|1403|1404|1405|1407|1409|1411|1412|1501|1502|1503|1511|"
    r"1521|1601|1602|1603|1611|1621|1701|1702|1703|1704|1705|1706|1707|1711|"
    r"1721)\b"
)
_KNOWLEDGE_NOTICES = {
    "NO_VALID_DOCUMENTS": ("没有可用且已就绪的上传文档", "以上回答不基于上传资料"),
    "NO_MATCH": ("没有检索到与本问题直接相关的依据", "以上回答不基于上传资料"),
    "THRESHOLD_FILTERED": ("相关度不足", "以上回答不基于上传资料"),
    "DEGRADED": ("知识检索不可用", "无法确认上传资料"),
}


@dataclass
class ExecOutcome:
    actual: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None
    status_hint: str | None = None
    status_reason: str = ""
    llm_records: list[dict[str, Any]] = field(default_factory=list)
    retriever_name: str = ""
    cleanup_ok: bool = True
    cleanup_error: str | None = None

    @property
    def error_dict(self) -> dict[str, str] | None:
        if self.error is None:
            return None
        return {
            "type": type(self.error).__name__,
            "message": str(self.error)[:1000],
        }


async def execute(case: EvalCase, *, fixture_raw: dict, fixture_path: Path) -> ExecOutcome:
    if case.execution_mode == "designed_only":
        return ExecOutcome(
            actual={"designedOnly": True, "reason": "需要 Spring Boot / MySQL / SSE 环境"},
            status_hint="SKIPPED",
            status_reason="Phase 1 执行器不连后端数据库；已登记为后续 E2E 用例。",
        )

    try:
        settings = _build_settings(case, fixture_path)
    except Exception as exc:  # noqa: BLE001
        return ExecOutcome(
            actual={"settingsError": str(exc)[:500]},
            error=exc,
            status_hint="BLOCKED",
            status_reason="无法构造评测配置",
        )

    if case.llm == "real" and not (settings.llm_provider == "openai" and settings.llm_api_key):
        return ExecOutcome(
            actual={
                "realModelConfigured": False,
                "provider": settings.llm_provider,
                "hasApiKey": bool(settings.llm_api_key),
            },
            status_hint="BLOCKED",
            status_reason="真实模型不可用：agent/.env 未配置 openai provider 或 API Key。",
        )

    inner = _build_inner_adapter(case, settings)
    adapter = RecordingLLMAdapter(
        inner,
        case_id=case.id,
        provider=settings.llm_provider,
        model=settings.llm_model if case.llm == "real" else inner.name,
    )
    request = _build_request(case)
    retriever = make_retriever(
        fixture_raw,
        user_id=int(case.request["userId"]),
        space_id=int(case.request["learningSpaceId"]),
        profile=case.profile,
        memory_top_k=settings.memory_top_k,
        knowledge_top_k=settings.knowledge_top_k,
    )
    runtime = AgentRuntime(settings, adapter)

    outcome = ExecOutcome(retriever_name=retriever.name)
    timeout = 90.0 if case.llm == "real" else DEFAULT_TIMEOUT
    try:
        actual: dict[str, Any]
        if case.probe == "auth_probe":
            actual = await _auth_probe(case, runtime, request)
        elif case.probe == "auto_fallback":
            actual = await _auto_fallback(case, runtime, request, fixture_path)
        elif case.probe == "retriever_guard":
            actual = await _retriever_guard(case, retriever)
        elif case.probe == "context":
            prepared = await asyncio.wait_for(
                runtime.prepare(request, retriever=retriever), timeout=timeout
            )
            actual = _actual_from_prepared(prepared, retriever, adapter)
        elif case.probe == "analyze":
            actual = await _analyze(case, runtime, request, retriever, adapter)
        else:
            actual = await _chat(
                case, runtime, request, retriever, adapter, timeout=timeout
            )
        outcome.actual = actual
        generation_error = actual.get("error") if isinstance(actual, dict) else None
        answer = actual.get("answer") if isinstance(actual, dict) else None
        answer_length = len(str(answer.get("text", ""))) if isinstance(answer, dict) else 0
        if (
            case.llm == "real"
            and isinstance(generation_error, dict)
            and generation_error.get("type") == "LLMError"
        ):
            outcome.status_hint = "BLOCKED"
            outcome.status_reason = (
                "真实模型连续返回空流/调用失败；这是上游可用性问题，"
                "不作为 Agent 回答质量结论。"
            )
        elif case.llm == "real" and answer_length < 10:
            outcome.status_hint = "BLOCKED"
            outcome.status_reason = (
                "真实模型三次尝试只返回不足 10 个字符；这是上游可用性问题，"
                "不作为 Agent 回答质量结论。"
            )
    except BaseException as exc:  # noqa: BLE001 —— 失败要落报告，不能中断整批
        outcome.error = exc
        outcome.actual = {
            "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
            "traceback": _short_traceback(exc),
            "llm": _llm_actual(adapter),
            "retrieval": _retrieval_actual(adapter, retriever),
        }
        if case.llm == "real" and isinstance(exc, LLMError):
            outcome.status_hint = "BLOCKED"
            outcome.status_reason = "真实模型调用失败；这不是 Agent 行为结论。"
    finally:
        try:
            await adapter.aclose()
        except Exception as exc:  # noqa: BLE001
            outcome.cleanup_ok = False
            outcome.cleanup_error = f"adapter.aclose: {type(exc).__name__}: {exc}"
        try:
            await retriever.aclose()
        except Exception as exc:  # noqa: BLE001
            outcome.cleanup_ok = False
            outcome.cleanup_error = (
                (outcome.cleanup_error + "; " if outcome.cleanup_error else "")
                + f"retriever.aclose: {type(exc).__name__}: {exc}"
            )
        if not retriever.closed:
            outcome.cleanup_ok = False
            outcome.cleanup_error = (
                (outcome.cleanup_error + "; " if outcome.cleanup_error else "")
                + "retriever.closed 未置位"
            )
        outcome.actual["cleanup"] = {
            "ok": outcome.cleanup_ok,
            "error": outcome.cleanup_error,
            "retrieverClosed": retriever.closed,
            "mode": case.cleanup,
        }
        outcome.llm_records = [record.as_dict() for record in adapter.records]
        outcome.actual["llm"] = _llm_actual(adapter)
        outcome.actual["retrieval"] = _merge_retrieval(
            outcome.actual.get("retrieval") or {}, retriever
        )
    return outcome


# ===================== 各 probe =====================


async def _chat(
    case: EvalCase,
    runtime: AgentRuntime,
    request: ChatRequest,
    retriever,
    adapter: RecordingLLMAdapter,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    prepared = await asyncio.wait_for(
        runtime.prepare(request, retriever=retriever), timeout=timeout
    )
    deltas: list[str] = []
    generation_error: BaseException | None = None
    # 真实模型偶尔 200 + 空流（上游瞬时问题）。重试有上限，重试记录都留在
    # RecordingLLMAdapter 里；三次仍空才判 BLOCKED，而不是把上游抖动算成
    # Agent 的回答质量失败。
    attempts = 3 if case.llm == "real" else 1
    for attempt in range(attempts):
        deltas = []
        generation_error = None
        try:
            async for delta in runtime.generate(prepared):
                deltas.append(delta)
        except BaseException as exc:  # noqa: BLE001
            generation_error = exc
        produced = len("".join(deltas))
        if case.llm != "real":
            if deltas or prepared.state.response:
                break
        elif produced >= 10 or len(prepared.state.response) >= 10:
            break

    citations = None
    if generation_error is None:
        citations = runtime.validate_citations(prepared)

    actual = _actual_from_prepared(prepared, retriever, adapter)
    text = prepared.state.response or "".join(deltas)
    guard = dict(_trace_detail(prepared.trace, "HISTORY_GUARD"))
    knowledge_guard = dict(_trace_detail(prepared.trace, "EVIDENCE_GUARD"))
    # 证据状态说明的最终结果：模型自己说了，还是 Guard 追加的，或者两者都没有。
    # 这不是「Guard 必须 added」；模型已经正确说明时，Guard 不重复追加是正确行为。
    status = knowledge_guard.get("status") or (
        (actual.get("retrieval") or {}).get("knowledgeStatus")
    )
    if status:
        notice = status_notice(str(status))
        final_discloses = already_discloses(str(status), text) or bool(
            notice and notice in text
        )
        duplicate = bool(notice) and text.count(notice) > 1
        knowledge_guard.update(
            {
                "status": status,
                "finalDiscloses": final_discloses,
                "duplicateNotice": duplicate,
                "addedOrDisclosed": bool(knowledge_guard.get("added")) or final_discloses,
                "outcome": (
                    "added"
                    if knowledge_guard.get("added")
                    else ("already_disclosed" if final_discloses else "missing")
                ),
            }
        )
    actual["answer"] = {
        "text": text,
        "length": len(text),
        "internalIdLeak": bool(_INTERNAL_ID_RE.search(text)),
        "historyGuard": guard,
        "evidenceGuard": knowledge_guard,
        "masteryClaim": analyze_mastery_claim(text),
        "citations": citations.as_dict() if citations else None,
    }
    actual["answer"]["historyIssueKinds"] = [
        item.get("kind") for item in actual["answer"]["historyGuard"].get("issues", [])
    ]
    if generation_error is not None:
        actual["error"] = {
            "type": type(generation_error).__name__,
            "message": str(generation_error)[:1000],
        }
    return actual


async def _analyze(
    case: EvalCase,
    runtime: AgentRuntime,
    request: AnalyzeRequest,
    retriever,
    adapter: RecordingLLMAdapter,
) -> dict[str, Any]:
    import app.analysis.analyzer as analyzer_module

    original = analyzer_module.build_retriever
    analyzer_module.build_retriever = lambda *args, **kwargs: retriever  # type: ignore[assignment]
    try:
        result = await asyncio.wait_for(
            runtime.analyze(request), timeout=DEFAULT_TIMEOUT
        )
    finally:
        analyzer_module.build_retriever = original  # type: ignore[assignment]

    return {
        "analysis": _flatten_analysis(result),
        "llm": _llm_actual(adapter),
        "retrieval": _retrieval_actual(adapter, retriever),
    }


async def _auth_probe(
    case: EvalCase,
    runtime: AgentRuntime,
    request: ChatRequest,
) -> dict[str, Any]:
    """用 FastAPI TestClient 打真实路由，验证「拒绝/放行」两种结果。

    这不是检查路由装饰器：请求经过 ``app.main.app`` 的依赖解析与状态机。
    为了让「未携带凭据 -> 401」可验证，必须给 Agent 显式配置一个**仅用于
    本机测试**的内部 token；生产/开发环境未配置 token 时接口是 fail-closed
    503，那是另一种安全状态，不能替代鉴权断言。
    """
    import os

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    probe_token = "phase1-internal-token-not-a-secret"
    env_overrides = {
        "AILS_INTERNAL_TOKEN": probe_token,
        "AILS_LLM_PROVIDER": "mock",
        "AILS_RETRIEVER_MODE": "fixture",
        "AILS_FIXTURE_PATH": str(AGENT_ROOT / "fixtures" / "demo_space.json"),
        "AILS_EMBEDDING_PROVIDER": "hash",
        "AILS_EMBEDDING_DIMENSION": "8",
        # 不允许评测意外连到开发机上的 Qdrant。
        "AILS_QDRANT_URL": "http://127.0.0.1:1",
    }
    previous_env = {key: os.environ.get(key) for key in env_overrides}
    os.environ.update(env_overrides)
    payload = {
        "runId": request.run_id,
        "traceId": request.trace_id or "phase1-auth",
        "userId": request.user_id,
        "conversationId": request.conversation_id,
        "learningSpaceId": request.learning_space_id,
        "userMessage": request.user_message,
        "history": [],
    }
    forged_payload = {**payload, "userId": 9001}
    headers = {
        "X-Internal-Token": probe_token,
        "X-Internal-User-Id": str(request.user_id),
    }
    try:
        # 演示 fixture 与 app 自身的 FixtureRetriever 形状一致；评测注入的
        # EvalRetriever 形状不同，不能直接塞给真实路由。
        probe_settings = Settings(
            _env_file=None,
            retriever_mode="fixture",
            fixture_path=str(AGENT_ROOT / "fixtures" / "demo_space.json"),
            llm_provider="mock",
        )
        probe_runtime = AgentRuntime(probe_settings, runtime._llm)  # noqa: SLF001
        with TestClient(fastapi_app) as client:
            previous_runtime = fastapi_app.state.runtime
            fastapi_app.state.runtime = probe_runtime
            try:
                chat = client.post("/agent/chat", json=payload)
                preview = client.post("/agent/context/preview", json=payload)
                wrong_token = client.post(
                    "/agent/chat",
                    json=payload,
                    headers={"X-Internal-Token": "wrong-token", "X-Internal-User-Id": str(request.user_id)},
                )
                forged = client.post("/agent/chat", json=forged_payload, headers=headers)
                diagnostics = client.get("/health/diagnostics")
                bound_preview = client.post(
                    "/agent/context/preview", json=payload, headers=headers
                )
                bound_diagnostics = client.get("/health/diagnostics", headers=headers)
                health = client.get("/health")
            finally:
                fastapi_app.state.runtime = previous_runtime
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    chat_status = chat.status_code
    preview_status = preview.status_code
    return {
        "auth": {
            "chatStatus": chat_status,
            "previewStatus": preview_status,
            "healthStatus": health.status_code,
            "healthBody": health.json(),
            "chatRequiresAuth": chat_status in (401, 403),
            "previewRequiresAuth": preview_status in (401, 403),
            "wrongTokenStatus": wrong_token.status_code,
            "wrongTokenRejected": wrong_token.status_code in (401, 403),
            "forgedBodyStatus": forged.status_code,
            "arbitraryUserIdAccepted": forged.status_code == 200,
            "boundPreviewStatus": bound_preview.status_code,
            "boundPreviewAccepted": bound_preview.status_code == 200,
            "diagnosticsStatus": diagnostics.status_code,
            "diagnosticsProtected": diagnostics.status_code in (401, 403),
            "boundDiagnosticsStatus": bound_diagnostics.status_code,
            "requestUserId": request.user_id,
            "authorizationHeaderSent": False,
            "chatBodyPrefix": chat.text[:120],
        },
        "llm": _llm_actual(runtime._llm),  # noqa: SLF001 —— 评测需要拿到记录
        "retrieval": {"retriever": runtime._settings.retriever_mode},  # noqa: SLF001
    }


async def _auto_fallback(
    case: EvalCase,
    runtime: AgentRuntime,
    request: ChatRequest,
    fixture_path: Path,
) -> dict[str, Any]:
    """auto 模式、无凭据时，真实 build_retriever 会静默返回 Fixture。

    这里刻意用 ``phase1_auto_space.json``（旧版单空间形状），因为走的是产品
    自带的 ``FixtureRetriever``，而不是评测注入的 ``EvalRetriever``。
    """
    auto_fixture = fixture_path.with_name("phase1_auto_space.json")
    if not auto_fixture.exists():
        auto_fixture = fixture_path
    auto_settings = Settings(
        _env_file=None,
        retriever_mode="auto",
        llm_provider="mock",
        fixture_path=str(auto_fixture),
        max_context_tokens=runtime._settings.max_context_tokens,  # noqa: SLF001
        recent_history_limit=runtime._settings.recent_history_limit,  # noqa: SLF001
    )
    # 不注入 retriever：让 prepare 走产品自己的 build_retriever，才能验证
    # 「auto + 无凭据」在真实调用链上会解析成什么。
    auto_runtime = AgentRuntime(auto_settings, runtime._llm)  # noqa: SLF001
    prepared = await asyncio.wait_for(
        auto_runtime.prepare(request), timeout=DEFAULT_TIMEOUT
    )
    retrieve = _trace_detail(prepared.trace, "RETRIEVE")
    metadata = prepared.as_dict()
    return {
        "autoFallback": {
            **metadata,
            "degradedFlag": metadata["degraded"],
            "retrieverClass": prepared.retriever_class,
            "traceSource": retrieve.get("source"),
            "warning": prepared.fallback_reason,
        },
        "context": _context_actual(prepared),
        "retrieval": {
            "retriever": prepared.retriever_name,
            **_state_retrieval(prepared.state),
        },
        "llm": _llm_actual(runtime._llm),  # noqa: SLF001
    }


async def _retriever_guard(case: EvalCase, injected_retriever) -> dict[str, Any]:
    """直接验证 ``build_retriever`` 的降级契约，不经过 Agent。

    分析链路会写长期认知，因此调用方传 ``allow_degradation=False``。Fixture
    模式必须被这个开关拦住；否则「样例概念被写进真实用户」只是靠调用方自觉。
    """
    del injected_retriever  # 这个 probe 要测的是产品工厂，不是评测替身
    settings = Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        fixture_path=str(AGENT_ROOT / "fixtures" / "demo_space.json"),
    )
    raised = False
    error_type = None
    message = None
    retriever_name = None
    retriever_class = None
    try:
        retriever = build_retriever(
            settings, None, allow_degradation=False, user_id=9001
        )
        retriever_name = retriever.name
        retriever_class = type(retriever).__name__
        await retriever.aclose()
    except RetrievalError as exc:
        raised = True
        error_type = type(exc).__name__
        message = str(exc)[:500]
    return {
        "guard": {
            "raised": raised,
            "errorType": error_type,
            "message": message,
            "retrieverName": retriever_name,
            "retrieverClass": retriever_class,
            "mode": settings.retriever_mode,
            "allowDegradation": False,
        },
        "llm": {"classification": "MOCK", "callCount": 0},
        "retrieval": {"retriever": retriever_name or "blocked"},
    }


# ===================== actual 构造 =====================


def _actual_from_prepared(prepared, retriever, adapter) -> dict[str, Any]:
    state = prepared.state
    return {
        "run": {
            "runId": state.run_id,
            "userId": state.user_id,
            "spaceId": state.space_id,
            "message": state.user_message,
            "historyReviewRequested": state.history_review_requested,
            "conceptNames": list(state.concept_names),
            "conceptIds": list(state.concept_ids),
        },
        "context": _context_actual(prepared),
        "retrieval": {
            **_retrieval_actual(adapter, retriever),
            **_state_retrieval(state),
        },
        "trace": {
            "eventTypes": prepared.trace.event_types(),
            "retrieve": _trace_detail(prepared.trace, "RETRIEVE"),
            "contextBuilt": _trace_detail(prepared.trace, "CONTEXT_BUILT"),
        },
        "isolation": _isolation_actual(retriever),
        "llm": _llm_actual(adapter),
        "fixture": {
            "profile": getattr(retriever, "_profile", ""),
            "userIds": [getattr(getattr(retriever, "data", None), "user_id", None)],
            "spaceIds": [getattr(getattr(retriever, "data", None), "space_id", None)],
        },
    }


def _context_actual(prepared) -> dict[str, Any]:
    context = prepared.context
    sections = context.sections
    dropped = context.dropped_sections
    cognitive_kinds = {"session", "understanding", "trajectory", "memory"}
    cognitive = [item for item in sections if _kind(item.name) in cognitive_kinds]
    false_no_history = (
        "本次没有检索到任何历史认知记录" in context.system_instruction
        and any(item.has_content for item in prepared.state.understandings)
    )
    return {
        "estimatedTokens": context.estimated_tokens,
        "contextTokens": context.context_tokens,
        "budgetTokens": context.budget_tokens,
        "budgetExceeded": context.budget_exceeded,
        "budgetOverrunReason": context.budget_overrun_reason,
        "systemInstruction": context.system_instruction,
        "messages": [message.as_dict() for message in context.messages],
        "sectionNames": [item.name for item in sections],
        "sectionTexts": {item.name: item.content for item in sections},
        "sectionPriorities": {item.name: item.priority for item in sections},
        "droppedSections": list(dropped),
        "warnings": list(context.warnings),
        "historySectionCount": sum(
            1 for item in sections if _kind(item.name) == "history"
        ),
        "hasSessionSection": any(_kind(item.name) == "session" for item in sections),
        "falseNoHistoryClaim": false_no_history,
        "cognitiveSectionsDropped": any(
            _kind(item.name) in cognitive_kinds and item.dropped for item in sections
        ),
        "cognitiveSectionNames": [item.name for item in cognitive],
    }


def _state_retrieval(state) -> dict[str, Any]:
    """本轮**实际**检索到的内容；与 fixture 配置分开，避免把「配置里有」当成「进了上下文」。"""
    return {
        "knowledgeStatus": state.knowledge_status,
        "knowledgeWarning": state.knowledge_warning,
        "knowledgeChunkIds": [item.id for item in state.knowledge],
        "knowledgeDocumentNames": [item.document_name for item in state.knowledge],
        "knowledgeChunkCount": len(state.knowledge),
        "understandingIds": [item.id for item in state.understandings],
        "understandingConceptIds": [item.concept_id for item in state.understandings],
        "understandingStatuses": [item.status.value for item in state.understandings],
        "memoryIds": [item.id for item in state.memories],
        "memoryScopes": [item.scope_type.value for item in state.memories],
        "memoryTypes": [item.memory_type.value for item in state.memories],
        "memoryContents": [item.content[:160] for item in state.memories],
        "evidenceIds": [item.id for item in state.evidences],
        "evidenceTypes": [item.evidence_type.value for item in state.evidences],
        "evidenceCorrectnesses": [item.correctness.value for item in state.evidences],
        "evidenceSources": [
            [source.source_type for source in item.sources] for item in state.evidences
        ],
        "trajectoryConcepts": [item.concept_name for item in state.trajectories],
        "trajectoryRevisionNos": [
            revision.revision_no
            for item in state.trajectories
            for revision in item.revisions
        ],
        "trajectoryEvidenceIds": [
            evidence.id for item in state.trajectories for evidence in item.evidences
        ],
        "historyReviewRequested": state.history_review_requested,
    }


def _retrieval_actual(adapter, retriever) -> dict[str, Any]:
    data = getattr(retriever, "data", None)
    if data is None:
        return {"retriever": getattr(retriever, "name", "")}
    return {
        "retriever": retriever.name,
        "conceptIds": [item.id for item in data.concepts],
        "conceptNames": [item.label for item in data.concepts],
        "understandingIds": [item.id for item in data.understandings],
        "understandingConceptIds": [item.concept_id for item in data.understandings],
        "understandingStatuses": [item.status.value for item in data.understandings],
        "memoryIds": [item.id for item in data.memories],
        "memoryScopes": [item.scope_type.value for item in data.memories],
        "memoryTypes": [item.memory_type.value for item in data.memories],
        "memoryContents": [item.content[:120] for item in data.memories],
        "knowledgeChunkIds": [item.id for item in data.knowledge],
        "knowledgeDocumentNames": [item.document_name for item in data.knowledge],
        "knowledgeChunkCount": len(data.knowledge),
        "evidenceIds": [item.id for item in data.evidences],
        "evidenceTypes": [item.evidence_type.value for item in data.evidences],
        "evidenceCorrectnesses": [item.correctness.value for item in data.evidences],
        "evidenceSources": [
            [source.source_type for source in item.sources] for item in data.evidences
        ],
        "revisionNos": [item.revision_no for item in data.revisions],
        "revisionConceptIds": [item.concept_id for item in data.revisions],
        "documents": [dict(item) for item in data.documents],
    }


def _merge_retrieval(base: dict[str, Any], retriever) -> dict[str, Any]:
    if not base:
        base = _retrieval_actual(None, retriever)
    base["calls"] = [call.as_dict() for call in getattr(retriever, "calls", [])]
    base["isolationViolations"] = list(
        getattr(retriever, "isolation_violations", [])
    )
    return base


def _isolation_actual(retriever) -> dict[str, Any]:
    data = getattr(retriever, "data", None)
    return {
        "boundUserId": getattr(data, "user_id", None),
        "boundSpaceId": getattr(data, "space_id", None),
        "violations": list(getattr(retriever, "isolation_violations", [])),
        "violationCount": len(getattr(retriever, "isolation_violations", [])),
        "callCount": len(getattr(retriever, "calls", [])),
        "retrieverName": getattr(retriever, "name", ""),
    }


def _llm_actual(adapter) -> dict[str, Any]:
    records = [record.as_dict() for record in getattr(adapter, "records", [])]
    first = records[0] if records else {}
    diagnostics = [
        item.get("streamDiagnostics") or item.get("completionDiagnostics") or {}
        for item in records
    ]
    diagnostics = [item for item in diagnostics if item]
    return {
        "classification": getattr(adapter, "classification", "UNKNOWN"),
        "adapterName": getattr(adapter, "name", ""),
        "provider": first.get("provider"),
        "model": first.get("model"),
        # Phase 3 要求每次调用都能核对输出预算，不能只写在用例文件里。
        # 这些是适配器实际持有的参数；Mock 适配器没有该属性时为 None。
        "settings": {
            "temperature": getattr(adapter, "_temperature", None),
            "maxTokens": getattr(adapter, "_max_tokens", None),
            "maxAttempts": getattr(adapter, "_max_attempts", None),
            "streaming": getattr(adapter, "classification", "") == "REAL_LLM",
        },
        "callCount": len(records),
        "callEmitted": any(item.get("callEmitted") for item in records),
        "outputEmitted": any(item.get("outputEmitted") for item in records),
        "httpStatuses": [item.get("httpStatus") for item in records],
        # 有诊断时才出现字段，Mock 调用不会凭空多出“空流”统计。
        "emptyOutputCalls": sum(
            1
            for item in diagnostics
            if item.get("errorCategory") in ("empty_stream", "empty_completion")
        ),
        "retriedCalls": sum(
            1
            for item in records
            if len((item.get("streamDiagnostics") or {}).get("attemptHistory") or []) > 1
        ),
        "diagnosticErrorCategories": [
            item.get("errorCategory") for item in diagnostics
        ],
        "diagnosticFinishReasons": [item.get("finishReason") for item in diagnostics],
        "diagnostics": diagnostics,
        "records": records,
    }


def _flatten_analysis(result) -> dict[str, Any]:
    evidence = result.evidence_candidates
    memories = result.memory_candidates
    proposal = result.understanding_proposal
    decision = result.verification_decision
    return {
        "detectedConcepts": list(result.detected_concepts),
        "analyzerVersion": result.analyzer_version,
        "evidence": {
            "count": len(evidence),
            "conceptIds": [item.concept_id for item in evidence],
            "types": [item.evidence_type.value for item in evidence],
            "correctness": [item.correctness.value for item in evidence],
            "observations": [item.observation for item in evidence],
        },
        "memory": {
            "count": len(memories),
            "scopes": [item.scope_type.value for item in memories],
            "conceptIds": [item.concept_id for item in memories],
            "spaceIds": [item.learning_space_id for item in memories],
            "types": [item.memory_type.value for item in memories],
            "contents": [item.content for item in memories],
            "actions": [item.action.value for item in memories],
        },
        "proposal": {
            "present": proposal is not None,
            "conceptId": proposal.concept_id if proposal else None,
            "knownParts": list(proposal.known_parts) if proposal else [],
            "uncertainParts": list(proposal.uncertain_parts) if proposal else [],
            "misconceptions": list(proposal.misconceptions) if proposal else [],
            "latestUserExplanation": proposal.latest_user_explanation if proposal else None,
        },
        "verification": {
            "action": decision.action.value,
            "trigger": decision.trigger.value if decision.trigger else None,
            "conceptId": decision.concept_id,
            "questionType": decision.question_type.value if decision.question_type else None,
            "questionText": decision.question_text,
            "reason": decision.reason,
        },
    }


# ===================== 构造 =====================


def _build_settings(case: EvalCase, fixture_path: Path) -> Settings:
    overrides = case.settings or {}
    common = {
        "retriever_mode": "fixture",
        "fixture_path": str(fixture_path),
        "max_context_tokens": int(overrides.get("maxContextTokens", 3000)),
        "recent_history_limit": int(overrides.get("recentHistoryLimit", 12)),
        "evidence_top_k": int(overrides.get("evidenceTopK", 3)),
        "knowledge_top_k": int(overrides.get("knowledgeTopK", 5)),
        "memory_top_k": int(overrides.get("memoryTopK", 5)),
    }
    if case.llm == "real":
        # Phase 3 需要在不改 agent/.env 的前提下做受控预算对照：600 是 Phase 1
        # 的固定值，不能被悄悄改掉；只有用例显式传 llmMaxTokens 时才覆盖。
        real_overrides = {
            "llm_temperature": float(overrides.get("llmTemperature", 0.1)),
            "llm_max_tokens": int(overrides.get("llmMaxTokens", 600)),
        }
        if overrides.get("llmTimeoutSeconds") is not None:
            real_overrides["llm_timeout_seconds"] = float(
                overrides["llmTimeoutSeconds"]
            )
        if overrides.get("llmModel"):
            real_overrides["llm_model"] = str(overrides["llmModel"])
        return Settings(
            _env_file=AGENT_ENV,
            llm_provider="openai",
            **common,
            **real_overrides,
        )
    return Settings(
        _env_file=None,
        llm_provider="mock",
        llm_model="mock",
        **common,
    )


def _build_inner_adapter(case: EvalCase, settings: Settings):
    if case.llm == "real":
        return build_llm_adapter(settings)
    if case.scripted_chat_sequence:
        return SequenceLLMAdapter(list(case.scripted_chat_sequence))
    if case.scripted_chat is not None:
        return ScriptedLLMAdapter(case.scripted_chat)
    if case.scripted_analyze is not None:
        return ScriptedLLMAdapter(case.scripted_analyze)
    return MockLLMAdapter()


def _build_request(case: EvalCase):
    raw = case.request
    history = [
        HistoryMessage(
            role=str(item["role"]).upper(), content=item.get("content", "")
        )
        for item in raw.get("history", [])
    ]
    session_raw = raw.get("session")
    session = SessionContext.model_validate(session_raw) if isinstance(session_raw, dict) else None
    common = {
        "run_id": int(raw.get("runId", 1)),
        "trace_id": raw.get("traceId", f"phase1-{case.id}"),
        "user_id": int(raw["userId"]),
        "conversation_id": int(raw["conversationId"]),
        "learning_space_id": int(raw["learningSpaceId"]),
        "user_message": str(raw["userMessage"]),
        "history": history,
        "session": session,
        "auth_token": None,
    }
    if case.probe == "analyze":
        return AnalyzeRequest(
            **common,
            message_id=raw.get("messageId"),
            assistant_message=raw.get("assistantMessage"),
        )
    return ChatRequest(**common)


# ===================== 工具 =====================


def _trace_detail(trace, event_type: str) -> dict[str, Any]:
    for event in reversed(trace.events):
        if event.type.value == event_type:
            return dict(event.detail)
    return {}


def _kind(name: str) -> str:
    return str(name).split(":", 1)[0]


def _short_traceback(exc: BaseException) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return "".join(lines[-6:])[:2000]
