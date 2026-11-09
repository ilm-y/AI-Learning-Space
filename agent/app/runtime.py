"""AgentRuntime —— 把各层串成一次运行。

对应 `agent-architecture.md` 第四十一节那条链路里，**同步路径**的那一半：

```text
ChatRequest
    ↓
AgentState 初始化            ← state/agent_state.py   （运行时状态，用完即弃）
    ↓
Concept 识别                 ← retriever/concept_detector.py
    ↓
Retrieve（U / M / K，按需 E） ← retriever/*
    ↓
Context Build                ← context/builder.py
    ↓
Tutor → LLM Adapter          ← tutor/tutor.py → llm/*
    ↓
SSE 推回后端
```

链路下游的「学习分析」（Evidence 抽取 → Understanding 提案 → Memory 候选 →
验证决策）走**另一条入口** `analyze()`，对应 `POST /agent/analyze`。
两条链路共用 LLM 适配器与 Retriever，但不共用上下文组装：教学要的是「怎么讲」，
分析要的是「这一轮暴露了什么」。

一条设计约束：Runtime 是**无状态**的。它持有的只有配置与 LLM 适配器，
每次请求自己开一个 Retriever、构造一个 AgentState、结束就丢。
跨对话的连续性全部来自后端数据，不来自进程内缓存 —— 否则多实例部署时
「同一个用户的两个对话」会各自看到不同的世界。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from app.analysis import LearningAnalyzer
from app.citation_filter import (
    CitationFilterReport,
    StreamingCitationFilter,
    filter_text,
)
from app.citations import (
    CitationReport,
    provided_labels_from_context,
    structured_citations,
    validate_citations,
)
from app.config import Settings
from app.context.builder import BuiltContext, ContextBuilder
from app.context.policy import (
    RetrievalPlan,
    is_history_review,
    plan_retrieval,
    wants_evidence,
)
from app.context.trajectory import (
    attach_evidences,
    build_trajectories,
    collect_evidence_ids,
)
from app.evidence_guard import GUARD_STATUSES, evidence_status_notice
from app.history_guard import (
    build_history_fallback,
    check_history_answer,
    retry_instruction,
)
from app.llm.base import LLMAdapter, LLMError, StreamCompletion
from app.retriever import (
    ConceptResolver,
    ResolverDecision,
    ResolverStatus,
    build_retriever,
    decide_resolver_call,
    detect_concepts,
)
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    RelatedConceptResult,
    RelatedConceptStatus,
    RetrievalError,
    normalize_memory_result,
    normalize_revision_result,
    normalize_understanding_result,
)
from app.retriever.knowledge import KnowledgeSearchService
from app.retriever.routing import FallbackRetriever
from app.schemas import (
    AnalyzeRequest,
    AnalyzeResult,
    ChatRequest,
    Concept,
    EvidenceItem,
    RelatedConcept,
)
from app.state.agent_state import AgentState, DetectedConcept
from app.trace import RunTrace, TraceEventType
from app.tutor import Tutor

log = logging.getLogger(__name__)

# 检索触发判断已迁到 app.context.policy（D4-2）。这里保留旧函数名作为别名，
# 因为 `tests/test_history_guard_runtime.py` 等调用方直接从 `app.runtime` 导入；
# 它们指向同一套规则，不存在第二份词表。
_is_history_review = is_history_review
_wants_evidence = wants_evidence

# 受控一跳扩展只认这两类关系。PREREQUISITE 只接受 INCOMING（关联概念是前置），
# RELATED 双向都算相关；其余类型即便后端显式返回也不扩张。
_RELATED_EVIDENCE_TYPES = ("PREREQUISITE", "RELATED")


@dataclass(slots=True)
class StreamEvent:
    """Agent → 调用方的一次流式事件。

    与后端对前端的 SSE 契约（message.start / delta / end / error）刻意不同名：
    这是 Backend 与 Agent 之间的**内网一跳**，混用同一套事件名会让排查时
    分不清一条事件是谁发的。后端接入时由 HttpPythonAgentGateway 做一次翻译。

    翻译表（后端 `AgentStreamHandler` 的三个回调）：

    | 这里 | 后端 |
    | --- | --- |
    | `meta`  | 无对应回调，`HttpPythonAgentGateway` 自己用（记录命中概念、上下文规模） |
    | `delta` | `onDelta(text)` |
    | `done`  | `onComplete(StreamCompletion)`；`truncated=true` 表示回答不完整 |
    | `error` | `onError(code, message)` |

    所以 `error` 的 data 里必须带 **int 类型的 code** —— `onError` 的签名收的是 int，
    只给 message 的翻译层只能自己编一个常量，等于把错误分类丢在内网这一跳上。
    取值约定见 `_ERROR_CODE_*`。
    """

    event: str  # meta | delta | done | error
    data: dict[str, object]


# error 事件的 code 取值。直接用 HTTP 语义，因为这一跳无论怎么实现都是 HTTP，
# 而且后端 message.error 的 code 字段本来就是一个 int（ChatService 里失败一侧用的就是 500/503）。
# 复盘回答是缓冲后推送的：按固定大小分块，保持 delta 语义，避免一条巨大的
# delta 让前端渲染出现整段跳变。它不改变事件类型或最终文本。
_HISTORY_STREAM_CHUNK = 120

_ERROR_CODE_INTERNAL = 500  # Agent 内部未预期异常
_ERROR_CODE_LLM = 502  # 模型上游（LLM Adapter）失败
_ERROR_CODE_RETRIEVAL = 503  # 后端检索不可用


@dataclass(slots=True)
class PreparedRun:
    """一次「检索 + 组装」的结果。chat 与 preview 共用同一段逻辑，两条路不会跑偏。"""

    state: AgentState
    context: BuiltContext
    retriever_name: str
    degraded: bool
    requested_mode: str = "auto"
    fallback_reason: str | None = None
    retriever_class: str = ""

    @property
    def trace(self) -> RunTrace:
        assert self.state.trace is not None
        return self.state.trace

    def as_dict(self) -> dict[str, object]:
        """结构化回退信息：调用方不依赖日志或「看一眼回答像不像样例」。"""
        return {
            "requestedMode": self.requested_mode,
            "resolvedSource": self.retriever_name,
            "degraded": self.degraded,
            "fallbackVisible": self.degraded,
            "fallbackReason": self.fallback_reason,
        }


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        llm: LLMAdapter,
        builder: ContextBuilder | None = None,
        knowledge_search: KnowledgeSearchService | None = None,
        concept_resolver: ConceptResolver | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._knowledge_search = knowledge_search
        # Resolver 与 Tutor 共用同一个 LLM 适配器；注入点只给测试 / 评测用，
        # 生产路径不应绕过配置里的阈值、超时与 Token 预算。
        self._resolver = concept_resolver or ConceptResolver(
            llm,
            confidence_threshold=settings.concept_resolver_confidence_threshold,
            max_per_field=settings.concept_resolver_max_concepts_per_field,
            timeout_seconds=settings.concept_resolver_timeout_seconds,
            max_tokens=settings.concept_resolver_max_tokens,
            max_prompt_tokens=settings.concept_resolver_max_prompt_tokens,
            max_candidates=settings.concept_resolver_max_candidate_concepts,
            history_turns=settings.concept_resolver_history_turns,
            history_chars_per_message=settings.concept_resolver_history_chars_per_message,
        )
        self._builder = builder or ContextBuilder(
            max_context_tokens=settings.max_context_tokens,
            recent_history_limit=settings.recent_history_limit,
        )
        self._tutor = Tutor(llm)
        # 分析链路与教学链路共用同一个 LLM 适配器（连接池、配置、Mock 行为一致），
        # 但各自持有独立的 prompt 与解析逻辑 —— 见 analysis/prompts.py 的说明。
        self._analyzer = LearningAnalyzer(settings, llm)

    @property
    def llm_name(self) -> str:
        return self._llm.name

    # ---------- 同步路径 ----------

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        trace = RunTrace(run_id=request.run_id, trace_id=request.trace_id)
        prepared: PreparedRun | None = None
        # 终态槽每次请求独立创建。适配器的 finish_reason 只能通过这一个对象
        # 进入 SSE，不能把截断信息丢在「最后一个 delta」或共享诊断字段里。
        completion = StreamCompletion()
        try:
            prepared = await self.prepare(request, trace)

            # 检索完成、开始生成之前给一条 meta：调用方由此知道这次命中了哪些概念、
            # 上下文多大，而不必等整段回答推完。调用方也可以直接忽略它。
            yield StreamEvent(
                "meta",
                {
                    "runId": request.run_id,
                    "detectedConcepts": prepared.state.concept_names,
                    "estimatedTokens": prepared.context.estimated_tokens,
                    "warnings": prepared.context.warnings,
                    "retrieverSource": prepared.retriever_name,
                    "degraded": prepared.degraded,
                    "fallbackReason": prepared.fallback_reason,
                },
            )

            async for delta in self.generate(prepared, completion=completion):
                yield StreamEvent("delta", {"delta": delta})

            # 非法来源已在 generate() 的流式过滤里被丢弃，这里的校验对象是
            # **实际交付文本**，正常应为 0 invalid；它仍写入 trace，供离线评估
            # 复核「过滤后是否还有漏网标注」。模型原本产出的越界标注记录在
            # CITATION_FILTER，不会因为交付文本被清洗而丢失证据。
            citation_report = self.validate_citations(prepared)

            # finish_reason=length 意味着模型因 Token 上限停止：已经产出的文本可以
            # 保留展示，但这一轮不能被当作正常完成。终态信息随 done 一起下发，
            # 由后端决定持久化状态并跳过只适合完整回答的后续分析。
            truncated = completion.truncated
            trace.record(
                TraceEventType.RUN_END,
                status="TRUNCATED" if truncated else "COMPLETED",
                finishReason=completion.finish_reason,
                truncated=truncated,
                responseChars=len(prepared.state.response),
            )
            yield StreamEvent(
                "done",
                {
                    "runId": request.run_id,
                    "detectedConcepts": prepared.state.concept_names,
                    "estimatedTokens": prepared.context.estimated_tokens,
                    "elapsedMs": trace.elapsed_ms,
                    "finishReason": completion.finish_reason,
                    "truncated": truncated,
                    # 结构化来源随终态一起交给后端。正文里的 `[资料《…》#…]`
                    # 仍然是事实上的展示标签，但客户端不再只能靠正则去猜：
                    # documentId / chunkId 才是可追溯、可校验权限的第一手字段。
                    "citations": structured_citations(citation_report),
                    # D4-4：把 CONTEXT_BUILT 已生成的 Manifest 随终态交给后端落库。
                    # 它仍严格保持 D4-3 的最小字段（来源 id / 状态 / 原因 / token），
                    # 不含正文、完整 prompt、query 或异常原文。放在 done 而不是 meta：
                    # 终态事件是后端唯一保证会消费的收尾信号；失败 run 不落 manifest，
                    # 是这一版已知的可观测边界。
                    "contextManifest": prepared.context.context_manifest,
                },
            )
        except (RetrievalError, LLMError) as exc:
            # 两类上游失败分开给码：调用方据此能区分「我的后端挂了」和「模型服务挂了」，
            # 而不是一律 500 —— 前者该去查后端，后者该去查模型供应商。
            code = _ERROR_CODE_RETRIEVAL if isinstance(exc, RetrievalError) else _ERROR_CODE_LLM
            trace.record(TraceEventType.RUN_ERROR, error=str(exc))
            log.warning("run %s 失败：%s", request.run_id, exc)
            yield StreamEvent(
                "error", {"runId": request.run_id, "code": code, "message": str(exc)}
            )
        except Exception as exc:  # noqa: BLE001 —— 入口必须兜住，否则 SSE 会静默断流
            trace.record(TraceEventType.RUN_ERROR, error=repr(exc))
            log.exception("run %s 未预期失败", request.run_id)
            yield StreamEvent(
                "error",
                {
                    "runId": request.run_id,
                    "code": _ERROR_CODE_INTERNAL,
                    "message": "Agent 内部错误",
                },
            )
        finally:
            if prepared is not None:
                log.info("run %s 结束：%s", request.run_id, prepared.trace.as_dict())

    async def generate(
        self,
        prepared: PreparedRun,
        *,
        completion: StreamCompletion | None = None,
    ) -> AsyncIterator[str]:
        """把一次已组装好的上下文交给模型，逐个产出增量。

        单独抽出来是为了让离线评估能复用同一条生成链路：评估需要拿到完整
        回答后做引用校验，而不想为了复用 `stream()` 再伪造一套 SSE。

        两条路径的分界只有一条：**显式复盘**回答需要先完成生成后校验再推送。
        普通对话仍然边生成边推送，不缓冲、不增加首字延迟。

        两条路径都会经过 `StreamingCitationFilter`：非法来源标注在离开
        Runtime 之前就被丢掉，而不是等 `validate_citations` 事后记一条 trace。
        普通对话只扣住「可能是标注开头」的尾巴，不缓冲整段回答；复盘路径
        本来就已经缓冲，直接对完整文本过滤。

        非成功检索状态下的兜底也在这里：模型输出结束后、`done` 之前，如果
        本轮没有知识片段且模型自己没说明资料状态，就追加一条固定说明。
        它是一个普通 delta，不删除、不覆盖模型已经产出的内容，
        因此 SSE 事件序列与引用校验入口都不变。
        """
        if prepared.state.history_review_requested:
            async for delta in self._generate_history_review(
                prepared, completion=completion
            ):
                yield delta
        else:
            citation_filter = StreamingCitationFilter(_provided_labels(prepared))
            async for delta in self._tutor.stream(
                prepared.context, completion=completion
            ):
                safe = citation_filter.feed(delta)
                if safe:
                    prepared.state.response += safe
                    yield safe
            tail = citation_filter.finish()
            if tail:
                prepared.state.response += tail
                yield tail
            self._record_citation_filter(prepared, citation_filter.report)

        # Guard 只看预算后的最终状态。SUCCESS_BUT_DROPPED / SUCCESS_PARTIAL 不在
        # GUARD_STATUSES 里：它们的说明已经在 Context Builder 阶段写进 Prompt，
        # 这里再追加「没有可用资料」会与 Context 的真实状态矛盾。
        status = prepared.context.knowledge_state
        if status not in GUARD_STATUSES:
            if status == "NOT_RETRIEVED" and not prepared.state.knowledge:
                # 本轮的 Policy 决定不查知识（例如纯寒暄）。这条 trace 只记录
                # 「没有发起检索」，不追加任何说明，避免把「没查」渲染成「没有资料」。
                prepared.trace.record(
                    TraceEventType.EVIDENCE_GUARD,
                    status=status,
                    queried=False,
                    added=False,
                    hasKnowledge=False,
                )
            return

        notice = None
        if not prepared.state.knowledge:
            notice = evidence_status_notice(
                status,
                prepared.state.user_message,
                prepared.state.response,
            )
        prepared.trace.record(
            TraceEventType.EVIDENCE_GUARD,
            status=status,
            added=bool(notice),
            hasKnowledge=bool(prepared.state.knowledge),
        )
        if notice:
            # 单独成段，避免和模型最后一句粘在一起被误读成同一句断言。
            addition = f"\n\n{notice}"
            prepared.state.response += addition
            yield addition

    async def _generate_history_review(
        self,
        prepared: PreparedRun,
        *,
        completion: StreamCompletion | None = None,
    ) -> AsyncIterator[str]:
        """复盘回答：先完整生成并校验，再推送给用户。

        首字延迟因此等于一次完整生成（需要重生成时再加一次），这是显式复盘
        独有的代价。普通对话不走这里，仍保持即时流式。

        允许一次受限重生成；重生成仍不通过（或重生成本身失败）时，使用由
        当前记录确定性拼出的回退文本，不把已确认无依据的断言推给用户。
        """
        state = prepared.state
        started = time.monotonic()
        first_completion = StreamCompletion()
        raw = await self._collect_history_answer(prepared, completion=first_completion)
        initial_issues = check_history_answer(raw, state)
        issues = initial_issues

        final = raw
        delivered_completion = first_completion
        regenerated = False
        regeneration_error: str | None = None
        remaining_issues: list = []
        fallback_used = False

        if issues:
            regenerated = True
            retry = ""
            retry_completion = StreamCompletion()
            try:
                retry = await self._collect_history_answer(
                    prepared,
                    extra_instruction=retry_instruction(issues),
                    completion=retry_completion,
                )
            except LLMError as exc:
                # 第一次调用已经成功，重生成失败时用确定性回退比把
                # 已知无依据的内容推出去更安全；调用次数仍只有两次。
                regeneration_error = str(exc)[:200]
            if retry:
                remaining_issues = check_history_answer(retry, state)
                if not remaining_issues:
                    final = retry
                    delivered_completion = retry_completion
                    issues = []
            if issues:
                final = build_history_fallback(state)
                # 回退文本是确定性拼出的完整内容，不继承被丢弃模型流的截断状态。
                delivered_completion = StreamCompletion(metadata_available=True)
                fallback_used = True

        if completion is not None:
            completion.copy_from(delivered_completion)

        # 复盘路径已经缓冲了完整回答，这里直接做一次完整过滤；
        # 与普通路径共用同一套「本轮实际注入来源」判定。
        final, filter_report = filter_text(final, _provided_labels(prepared))
        self._record_citation_filter(prepared, filter_report)

        state.response = final
        first_delta_ms = int((time.monotonic() - started) * 1000)
        prepared.trace.record(
            TraceEventType.HISTORY_GUARD,
            hasTrajectory=bool(state.trajectories),
            evidenceCount=len(
                {
                    evidence.id
                    for evidence in [
                        *[
                            item
                            for trajectory in state.trajectories
                            for item in trajectory.evidences
                        ],
                        *state.evidences,
                    ]
                }
            ),
            rawChars=len(raw),
            issues=[issue.as_dict() for issue in initial_issues],
            regenerated=regenerated,
            regenerationError=regeneration_error,
            remainingIssues=[issue.as_dict() for issue in remaining_issues],
            fallback=fallback_used,
            outputChars=len(final),
            firstDeltaElapsedMs=first_delta_ms,
        )
        for index in range(0, len(final), _HISTORY_STREAM_CHUNK):
            yield final[index : index + _HISTORY_STREAM_CHUNK]

    async def _collect_history_answer(
        self,
        prepared: PreparedRun,
        *,
        extra_instruction: str | None = None,
        completion: StreamCompletion | None = None,
    ) -> str:
        parts: list[str] = []
        async for delta in self._tutor.stream(
            prepared.context,
            extra_instruction=extra_instruction,
            completion=completion,
        ):
            parts.append(delta)
        return "".join(parts)

    def _record_citation_filter(
        self, prepared: PreparedRun, report: CitationFilterReport
    ) -> None:
        """把「被过滤掉什么」写进 trace。

        过滤后 `state.response` 里已经没有非法标注，`CITATION_CHECK` 会显示 0；
        真正的问题样本必须在 `CITATION_FILTER` 里可见，否则等于把证据藏起来。
        """
        if not report.removed:
            return
        prepared.trace.record(
            TraceEventType.CITATION_FILTER,
            droppedLabels=report.dropped,
            malformedLabels=report.malformed,
            removed=report.removed,
            providedLabels=report.provided_labels,
        )
        log.warning(
            "run %s 过滤非法来源标注：dropped=%s malformed=%s",
            prepared.state.run_id,
            report.dropped,
            report.malformed,
        )

    def validate_citations(self, prepared: PreparedRun) -> CitationReport:
        """校验本轮回答里的来源标注，并把结果写进 trace。

        校验本身失败（比如某些异常实现返回了非字符串）不应影响已经生成的回答，
        所以入口兜住异常，只记录一条带 error 的 trace。
        """
        try:
            labels = _provided_labels(prepared)
            report = validate_citations(
                prepared.state.response,
                prepared.state.knowledge,
                provided_labels=labels,
            )
        except Exception as exc:  # noqa: BLE001 —— 观测失败不得打挂对话
            log.warning("引用校验失败（不影响回答）：%s", type(exc).__name__)
            prepared.trace.record(
                TraceEventType.CITATION_CHECK, error=type(exc).__name__
            )
            return CitationReport()

        prepared.trace.record(
            TraceEventType.CITATION_CHECK,
            total=report.total,
            valid=report.valid,
            invalid=report.invalid,
            illegalRate=report.illegal_rate,
            invalidLabels=report.invalid_labels,
            malformedLabels=report.malformed_labels,
            duplicateLabels=report.duplicate_labels,
            citedDocuments=report.cited_documents,
            providedLabels=labels,
            droppedKnowledge=sum(
                1
                for section in prepared.context.sections
                if section.name.startswith("knowledge:") and section.dropped
            ),
        )
        if report.invalid_labels or report.malformed_labels:
            log.warning(
                "run %s 引用校验未通过：invalid=%s malformed=%s",
                prepared.state.run_id,
                report.invalid_labels,
                report.malformed_labels,
            )
        return report

    # ---------- 分析路径 ----------

    async def analyze(
        self,
        request: AnalyzeRequest,
        *,
        retriever=None,
    ) -> AnalyzeResult:
        """从一轮已经完成的对话里抽取学习候选。

        与 `stream` 的关系是**前后脚**：后端在助手回复落库、SSE 结束后调用这里，
        拿到结果再写 Evidence / Understanding / Memory。它不产生用户可见的流，
        失败时由调用方决定是否退回本地规则（而不是把错误展示给用户）。

        `retriever` 只给评估/测试注入受控检索源；不传时仍按生产配置构造，
        保持分析链路的 ``allow_degradation=False``。传入的检索源归调用方关闭。
        """
        trace = RunTrace(run_id=request.run_id, trace_id=request.trace_id)
        trace.record(
            TraceEventType.RUN_START,
            mode="analyze",
            userId=request.user_id,
            conversationId=request.conversation_id,
            spaceId=request.learning_space_id,
            messageId=request.message_id,
            messageChars=len(request.user_message or ""),
        )
        try:
            return await self._analyzer.analyze(request, trace, retriever=retriever)
        except Exception as exc:
            trace.record(TraceEventType.RUN_ERROR, error=repr(exc))
            log.warning("analyze run %s 失败：%s", request.run_id, exc)
            raise
        finally:
            log.info("analyze run %s 结束：%s", request.run_id, trace.as_dict())

    async def prepare(
        self,
        request: ChatRequest,
        trace: RunTrace | None = None,
        *,
        retriever=None,
    ) -> PreparedRun:
        """检索 + 组装，不调用模型。preview 接口直接复用它。

        `retriever` 只给评估/测试注入受控检索源；不传时仍按配置构造，
        并且由本方法负责关闭。传入的检索源归调用方关闭。
        """
        trace = trace or RunTrace(run_id=request.run_id, trace_id=request.trace_id)
        state = AgentState.from_request(request, trace)
        trace.record(
            TraceEventType.RUN_START,
            userId=request.user_id,
            conversationId=request.conversation_id,
            spaceId=request.learning_space_id,
            sessionId=request.session.id if request.session else None,
            messageChars=len(request.user_message or ""),
        )

        owned_retriever = retriever is None
        if retriever is None:
            retriever = build_retriever(
                self._settings,
                request.auth_token,
                user_id=request.user_id,
                delegation_token=request.backend_identity,
                knowledge_search=self._knowledge_search,
            )
        try:
            await self._retrieve(state, retriever)
            state.context = self._builder.build(state)
            trace.record(
                TraceEventType.CONTEXT_BUILT,
                estimatedTokens=state.context.estimated_tokens,
                droppedSections=state.context.dropped_sections,
                # 检索层状态与预算后的最终状态必须同时可见：只看前者会把
                # 「检索成功但知识被全部丢弃」误读成模型拿到了资料。
                knowledgeStatus=state.knowledge_status,
                knowledgeState=state.context.knowledge_state,
                knowledgeInjected=state.context.knowledge_injected_chunk_ids,
                knowledgeDropped=state.context.knowledge_dropped_chunk_ids,
                warnings=state.context.warnings,
                # D4-3 最小 Manifest：只含 source_id / 检索与注入状态 / 原因 /
                # token 估算。不含正文、完整 prompt、query 或异常原文。
                contextManifest=state.context.context_manifest,
            )
        finally:
            if owned_retriever:
                await retriever.aclose()

        requested_mode, degraded, fallback_reason = _retriever_selection(
            retriever, self._settings
        )
        return PreparedRun(
            state=state,
            context=state.context,
            retriever_name=retriever.name,
            degraded=degraded,
            requested_mode=requested_mode,
            fallback_reason=fallback_reason,
            retriever_class=type(retriever).__name__,
        )

    # ---------- 检索 ----------

    async def _retrieve(self, state: AgentState, retriever) -> None:
        query = state.user_message or ""

        concepts = await retriever.list_concepts(state.space_id)
        trace_detail: dict[str, object] = {"concepts_available": len(concepts)}

        detected = detect_concepts(query, state.history, concepts)
        rule_detected = [
            DetectedConcept(concept=concept, score=score, matched_on=matched_on)
            for concept, score, matched_on in detected
        ]
        # 规则结果先就位：Resolver 只补充，不覆盖，也不改变规则命中的优先级。
        state.detected_concepts = list(rule_detected)

        # E2：按需调用。策略是确定性的，Resolver 不参与「要不要调用自己」。
        call_decision = decide_resolver_call(
            query,
            rule_hit_count=len(rule_detected),
            candidate_count=len(concepts),
            enabled=self._settings.concept_resolver_enabled,
        )
        state.resolver_intent = call_decision.intent
        state.resolver_call_reason = call_decision.reason

        resolver_decision: ResolverDecision | None = None
        if call_decision.call:
            # 一轮最多一次；超时 / 5xx / 非法 JSON 由 Resolver 内部降级，
            # 这里不重试、不抛出，主链路继续使用规则结果。
            resolver_decision = await self._resolver.resolve(
                run_id=state.run_id,
                user_id=state.user_id,
                space_id=state.space_id,
                user_message=query,
                history=state.history,
                candidates=concepts,
                allow_history_reference=call_decision.allow_history_reference,
            )
            _apply_resolver_decision(state, resolver_decision, concepts)

        state.trace.record(
            TraceEventType.CONCEPT_DETECT,
            detected=[item.concept.label for item in state.detected_concepts],
            matchedOn=[item.matched_on for item in state.detected_concepts],
            ruleDetected=[item.concept.label for item in rule_detected],
            resolverStatus=state.resolver_status,
            resolverIntent=state.resolver_intent,
            resolverCallReason=state.resolver_call_reason,
            **trace_detail,
        )
        if resolver_decision is not None:
            state.trace.record(
                TraceEventType.CONCEPT_RESOLVE,
                status=resolver_decision.status.value,
                intent=call_decision.intent,
                modelIntent=resolver_decision.intent,
                callReason=call_decision.reason,
                presentedConceptIds=resolver_decision.presented_concept_ids,
                currentConceptIds=state.resolver_current_concept_ids,
                relatedConceptIds=state.resolver_related_concept_ids,
                needsHistoryReference=state.resolver_needs_history_reference,
                dropped=resolver_decision.dropped,
                warning=state.resolver_warning,
                promptTokens=resolver_decision.prompt_tokens,
                completionTokens=resolver_decision.completion_tokens,
                tokenSource=resolver_decision.token_source,
                elapsedMs=resolver_decision.elapsed_ms,
            )

        concept_ids = state.concept_ids
        # D4-2：把「本轮查什么、为什么查」收敛成一份结构化计划。
        # Runtime 只执行计划，不在下面重新做触发判断；Session 的 goal/focus
        # 也由这里决定是否注入（SessionResolver 可能把新话题挂到旧 Session）。
        plan = plan_retrieval(
            user_message=query,
            concept_ids=concept_ids,
            session=state.session,
            space_concepts=concepts,
            history=state.history,
        )
        state.retrieval_plan = plan
        # 复盘校验比「读取轨迹」更窄：只有个人历史问题才缓冲回答。
        # 否则「过去十年 CPU 架构有什么变化」这类知识问题会因为一个「过去」
        # 被当成复盘，既增加首字延迟，又可能被要求说明「没有历史记录」。
        state.history_review_requested = plan.review_required
        _apply_session_policy(state, plan)

        # Knowledge 独立于 Concept：用户问一个还没建 Concept 的新知识点时，
        # 上传的文档仍然应该参与回答。Concept 只决定 U / M / E 是否有可挂靠对象。
        knowledge_query = KnowledgeQuery(
            user_id=state.user_id,
            space_id=state.space_id,
            question=query,
            concept_ids=tuple(concept_ids),
            top_k=self._settings.knowledge_top_k,
            min_score=self._settings.knowledge_min_score,
        )

        # REQUIRED / OPTIONAL 在本批都表示「发起检索」；SKIP 才表示不发起。
        # Policy 在没有概念锚点时已经把 U / M / E / Revision 统一置为 SKIP。
        retrieve_knowledge = plan.knowledge.triggered
        retrieve_understandings = plan.understanding.triggered and bool(concept_ids)
        retrieve_memories = plan.memory.triggered and bool(plan.memory_concept_ids)
        wants_evidence = plan.evidence.triggered and bool(concept_ids)
        # 轨迹只注入显式复盘问题；「之前学过链表，现在学树」这类普通问题
        # 即使带时间词，也不应该被塞进历史轨迹。
        retrieve_trajectory = plan.revision.triggered and bool(concept_ids)
        # 复盘时用 Revision 关联精确回查，而不是再塞一份「最近 3 条」，
        # 否则同一批证据会在上下文里出现两次。
        fetch_recent_evidence = wants_evidence and not retrieve_trajectory
        # 第一轮并行：Knowledge / U / M / 原始 Evidence / 相关概念互不等待。
        # 原始 Evidence **不依赖关系结果**，它必须和 U / M / K 同时发出；
        # 只有跨概念扩展才需要等关系结果。关系失败不得阻塞其余四路。
        task_names: list[str] = []
        tasks: list = []

        def _add(name: str, coro) -> None:
            task_names.append(name)
            tasks.append(coro)

        if retrieve_knowledge:
            _add("knowledge", self._safe_knowledge(retriever, knowledge_query))
        if retrieve_understandings:
            _add(
                "understandings",
                retriever.get_understandings(state.user_id, state.space_id, concept_ids),
            )
        if retrieve_memories:
            _add(
                "memories",
                retriever.get_memories(
                    state.user_id,
                    state.space_id,
                    list(plan.memory_concept_ids),
                    query,
                ),
            )
        if fetch_recent_evidence:
            # 原始概念的 Evidence 保持原有口径（evidence_top_k），不因扩展而挤占。
            _add(
                "evidences",
                self._safe_evidences(
                    retriever, state, concept_ids, self._settings.evidence_top_k
                ),
            )
        if fetch_recent_evidence and self._settings.evidence_related_concepts_limit > 0:
            # 相关概念查询自己负责把失败变成带 warning 的 FAILED 结果，
            # 绝不抛断整轮检索。
            _add("related", self._safe_related_concepts(retriever, state, concept_ids))

        results = await asyncio.gather(*tasks)
        retrieved = dict(zip(task_names, results))

        # 没有发起的检索保持 NOT_RETRIEVED / NOT_QUERIED，绝不能渲染成「查过但没有」。
        knowledge_result: KnowledgeResult | None = None
        if "knowledge" in retrieved:
            knowledge_result = retrieved["knowledge"]
            state.knowledge = knowledge_result.chunks
            state.knowledge_status = knowledge_result.status.value
            state.knowledge_warning = knowledge_result.warning

        if "understandings" in retrieved:
            understanding_result = normalize_understanding_result(
                retrieved["understandings"]
            )
            state.understandings = understanding_result.items
            state.understanding_status = understanding_result.status.value
            state.understanding_warning = understanding_result.warning
        if "memories" in retrieved:
            memory_result = normalize_memory_result(retrieved["memories"])
            state.memories = memory_result.items
            state.memory_status = memory_result.status.value
            state.memory_warning = memory_result.warning
            state.memory_detail = memory_result.detail

        if "related" in retrieved:
            related_result: RelatedConceptResult = retrieved["related"]
            state.related_concepts = related_result.items
            state.related_concepts_status = related_result.status.value
            state.related_concepts_warning = related_result.warning

        if "evidences" in retrieved:
            original_result: EvidenceRetrievalResult = retrieved["evidences"]
            state.evidences = list(original_result.items)
            state.evidence_status = original_result.status.value
            state.evidence_warning = original_result.warning_text

        if fetch_recent_evidence:
            # 扩展决策在关系结果返回后进行；原始 Evidence 已经并行取回，
            # 因此「扩展」不依赖、也不会拖慢本轮的基础证据检索。
            state.expanded_concepts = _select_expansion_concepts(
                concept_ids,
                state.related_concepts,
                self._settings.evidence_related_concepts_limit,
                # Resolver 的 related 只影响「先看哪些真实关系候选」，不能新增范围：
                # 不在 concept_relation 结果里的 id 永远不会被选中。
                hint_ids=set(state.resolver_related_concept_ids),
            )
            # 最多一跳、最多 N 个概念、独立的小预算（evidence_related_evidence_limit）。
            if state.expanded_concepts and self._settings.evidence_related_evidence_limit > 0:
                expanded_ids = [item.concept_id for item in state.expanded_concepts]
                cross_result = await self._safe_evidences(
                    retriever,
                    state,
                    expanded_ids,
                    self._settings.evidence_related_evidence_limit,
                )
                state.cross_concept_evidence_status = cross_result.status.value
                state.cross_concept_evidence_warning = cross_result.warning_text
                state.cross_concept_evidences = _mark_cross_concept_evidences(
                    cross_result.items,
                    state.expanded_concepts,
                    set(concept_ids),
                    state.evidences,
                )
                state.evidences.extend(state.cross_concept_evidences)

        if retrieve_trajectory:
            revision_result = normalize_revision_result(
                await retriever.get_revisions(
                    state.user_id, concept_ids, space_id=state.space_id
                )
            )
            state.revision_status = revision_result.status.value
            state.revision_warning = revision_result.warning
            state.trajectories = build_trajectories(
                state.understandings, revision_result.revisions
            )
            if state.trajectories:
                evidence_ids = collect_evidence_ids(
                    state.trajectories, state.understandings
                )
                if evidence_ids:
                    state.evidences = await retriever.get_evidence_details(
                        state.user_id, concept_ids, evidence_ids
                    )
                attach_evidences(state.trajectories, state.evidences)
            elif wants_evidence:
                # 没有 Revision 时仍保留原有按需召回，避免「复盘」比普通追问拿到更少信息。
                fallback_result = await self._safe_evidences(
                    retriever, state, concept_ids, self._settings.evidence_top_k
                )
                state.evidences = list(fallback_result.items)
                state.evidence_status = fallback_result.status.value
                state.evidence_warning = fallback_result.warning_text

        state.trace.record(
            TraceEventType.RETRIEVE,
            source=retriever.name,
            conceptIds=concept_ids,
            # D4-2：策略版本、意图与逐数据源的决策理由。它解释「为什么这轮查了 /
            # 没查某个源」，而不是只报告结果数量。
            retrievalPolicy=plan.as_dict(),
            intent=plan.intent.value,
            policyVersion=plan.policy_version,
            sessionInjected=plan.session.injects_frame,
            sessionPolicyReason=plan.session.reason,
            understandings=[item.concept_name for item in state.understandings],
            understandingStatus=state.understanding_status,
            understandingWarning=state.understanding_warning,
            memories=len(state.memories),
            memoryStatus=state.memory_status,
            memoryDetail=state.memory_detail,
            memoryWarning=state.memory_warning,
            revisionStatus=state.revision_status,
            revisionWarning=state.revision_warning,
            knowledge=len(state.knowledge),
            knowledgeStatus=state.knowledge_status,
            knowledgeCandidates=(
                knowledge_result.candidates if knowledge_result is not None else None
            ),
            evidences=len(state.evidences),
            evidenceStatus=state.evidence_status,
            evidenceWarning=state.evidence_warning,
            crossConceptEvidences=len(state.cross_concept_evidences),
            crossConceptEvidenceStatus=state.cross_concept_evidence_status,
            crossConceptEvidenceWarning=state.cross_concept_evidence_warning,
            evidenceRequested=wants_evidence,
            relatedConceptStatus=state.related_concepts_status,
            relatedConceptCandidates=[item.concept_id for item in state.related_concepts],
            expandedConceptIds=[item.concept_id for item in state.expanded_concepts],
            relatedConceptWarning=state.related_concepts_warning,
            resolverStatus=state.resolver_status,
            resolverRelatedHintIds=state.resolver_related_concept_ids,
            # 提示里有哪些 id 在真实关系结果中不存在：它们不会进入扩展，
            # 这条字段是「Resolver 不能绕过 concept_relation」的可观测证据。
            resolverHintsNotInRelation=sorted(
                set(state.resolver_related_concept_ids)
                - {item.concept_id for item in state.related_concepts}
            ),
            historyReviewRequested=state.history_review_requested,
            trajectoryConcepts=[item.concept_name for item in state.trajectories],
            trajectoryRevisions=sum(len(item.revisions) for item in state.trajectories),
            trajectoryEvidenceIds=[item.id for item in state.evidences],
        )

    async def _safe_knowledge(self, retriever, query: KnowledgeQuery) -> KnowledgeResult:
        """知识检索的故障边界：任何异常都变成带 warning 的空知识。"""
        try:
            result = await retriever.get_knowledge(query)
        except Exception as exc:  # noqa: BLE001 —— 普通检索失败不得打挂 /agent/chat
            log.warning(
                "知识检索未预期失败，降级为空：user=%s space=%s error=%s",
                query.user_id,
                query.space_id,
                type(exc).__name__,
            )
            return KnowledgeResult.degraded(
                "知识检索失败，本次回答未使用文档资料。"
            )
        if not isinstance(result, KnowledgeResult):
            log.warning("检索源返回了非 KnowledgeResult，按空知识处理")
            return KnowledgeResult.degraded(
                "知识检索返回格式异常，本次回答未使用文档资料。"
            )
        return result

    async def _safe_evidences(
        self,
        retriever,
        state: AgentState,
        concept_ids: list[int],
        limit: int,
    ) -> EvidenceRetrievalResult:
        """Evidence 检索的故障边界。

        失败必须变成 DEGRADED，而不是空列表：否则上下文会把「证据服务坏了」
        说成「用户没有历史证据」。DEGRADED 仍可携带部分已解析的条目。
        """
        try:
            result = await retriever.get_evidences(
                state.user_id, state.space_id, concept_ids, limit
            )
        except Exception as exc:  # noqa: BLE001 —— 证据检索失败不得打挂 /agent/chat
            log.warning(
                "证据检索未预期失败，标记降级：user=%s space=%s concepts=%s error=%s",
                state.user_id,
                state.space_id,
                concept_ids,
                type(exc).__name__,
            )
            return EvidenceRetrievalResult.degraded(
                f"证据检索未预期失败（spaceId={state.space_id}, "
                f"conceptIds={concept_ids}）：{type(exc).__name__}"
            )
        if isinstance(result, EvidenceRetrievalResult):
            return result
        if isinstance(result, list):
            # 兼容尚未升级的检索源：旧契约的空列表无法区分故障与无数据，
            # 只能按「成功空结果」处理，并把这一点记录在告警里。
            log.warning("检索源返回了旧式 Evidence 列表，无法判断降级状态")
            return EvidenceRetrievalResult.from_items(
                result, warnings=["检索源返回旧式 Evidence 列表，无法确认查询是否完整。"]
            )
        log.warning("检索源返回了非 EvidenceRetrievalResult，按降级处理")
        return EvidenceRetrievalResult.degraded(
            f"证据检索返回格式异常（spaceId={state.space_id}, conceptIds={concept_ids}）。"
        )

    async def _safe_related_concepts(
        self, retriever, state: AgentState, concept_ids: list[int]
    ) -> RelatedConceptResult:
        """相关概念查询的故障边界。

        失败必须是 FAILED，不能是 EMPTY —— 否则上下文会把「后端挂了」
        说成「这些概念没有关联」，误导用户。原始概念检索不受影响。
        """
        try:
            result = await retriever.get_related_concepts(
                state.user_id,
                state.space_id,
                concept_ids,
                relation_types=_RELATED_EVIDENCE_TYPES,
                limit=self._settings.evidence_related_concepts_limit,
            )
        except Exception as exc:  # noqa: BLE001 —— 扩展失败不得打挂 /agent/chat
            log.warning(
                "相关概念查询未预期失败，保持原始概念检索：user=%s space=%s concepts=%s error=%s",
                state.user_id,
                state.space_id,
                concept_ids,
                type(exc).__name__,
            )
            return RelatedConceptResult(
                items=[],
                status=RelatedConceptStatus.FAILED,
                warning="相关概念查询失败，本次回答未做跨概念证据扩展。",
            )
        if not isinstance(result, RelatedConceptResult):
            log.warning("检索源返回了非 RelatedConceptResult，按扩展失败处理")
            return RelatedConceptResult(
                items=[],
                status=RelatedConceptStatus.FAILED,
                warning="相关概念查询返回格式异常，本次回答未做跨概念证据扩展。",
            )
        return result

    async def aclose(self) -> None:
        await self._llm.aclose()


def _apply_resolver_decision(
    state: AgentState,
    decision: ResolverDecision,
    candidates: list[Concept],
) -> None:
    """把 Resolver 决策写进 state，并只合并通过服务端校验的 current 概念。

    - 规则已命中的 concept 不重复追加（规则优先）；
    - 候选表按 id 再核对一次：即便 Resolver 内部校验失效，这里也不会把
      跨 Space / 编造的 concept 塞进 `concept_ids`；
    - related 只记入 state，供 E1 排序提示，不进入检索范围。
    """
    state.resolver_status = decision.status.value
    state.resolver_warning = decision.warning
    state.resolver_current_concept_ids = [
        item.concept_id for item in decision.current_concepts
    ]
    state.resolver_related_concept_ids = [
        item.concept_id for item in decision.related_concepts
    ]
    state.resolver_needs_history_reference = decision.needs_history_reference
    state.resolver_prompt_tokens = decision.prompt_tokens
    state.resolver_completion_tokens = decision.completion_tokens
    state.resolver_token_source = decision.token_source
    state.resolver_elapsed_ms = decision.elapsed_ms
    state.resolver_dropped = list(decision.dropped)

    # 只有 SUCCESS 才可能带来可用的 current 概念；DEGRADED / FAILED 一律只保留规则结果。
    if decision.status is not ResolverStatus.SUCCESS:
        return
    by_id = {concept.id: concept for concept in candidates}
    known = {item.concept.id for item in state.detected_concepts}
    for resolved in decision.current_concepts:
        if resolved.concept_id in known:
            continue
        concept = by_id.get(resolved.concept_id)
        if concept is None:
            # 防御性兜底：正常路径下 Resolver 已按候选白名单过滤过。
            state.resolver_dropped.append(
                f"current_concepts 丢弃 conceptId={resolved.concept_id}：不在当前 Space 候选列表"
            )
            continue
        reason = resolved.reason.strip() or "语义匹配"
        state.detected_concepts.append(
            DetectedConcept(
                concept=concept,
                score=resolved.confidence,
                matched_on=f"resolver:{reason}",
            )
        )
        known.add(resolved.concept_id)


def _select_expansion_concepts(
    concept_ids: list[int],
    related: list[RelatedConcept],
    limit: int,
    *,
    hint_ids: set[int] | None = None,
) -> list[RelatedConcept]:
    """从后端返回的候选里挑出真正要查 Evidence 的关联概念。

    规则（固定，不做隐式猜测）：
    - 排除本轮已识别概念（原始概念优先，不重复查）；
    - 只认 PREREQUISITE / RELATED；
    - PREREQUISITE 只接受 INCOMING（source 是 target 的前置）；
    - RELATED 双向都算；
    - 按 concept_id 去重，保留首次出现（后端已按 weight 降序返回）；
    - 最多 `limit` 个。

    `hint_ids` 来自 E2 Resolver 的 `related_concepts`，**只做稳定排序提示**：
    命中的真实关系候选排到前面，用来帮助选择扩展起点。它不能新增候选、
    不能改变方向过滤、不能突破 `limit`；不在 `related` 里的 id 会被忽略。
    与 `concept_relation` 冲突时，以关系表为准。
    """
    if limit <= 0:
        return []
    original = set(concept_ids)
    candidates: list[RelatedConcept] = []
    seen: set[int] = set()
    for item in related:
        if item.concept_id in original or item.concept_id in seen:
            continue
        relation_type = (item.relation_type or "").upper()
        if relation_type not in _RELATED_EVIDENCE_TYPES:
            continue
        direction = (item.direction or "").upper()
        if relation_type == "PREREQUISITE" and direction != "INCOMING":
            # 当前概念的 OUTGOING 前置 = 当前概念是别人的前置，不是「它的前置」。
            continue
        seen.add(item.concept_id)
        candidates.append(item)
    if hint_ids:
        # list.sort 是稳定排序：同等条件下仍保持后端返回的 weight 顺序。
        candidates.sort(key=lambda item: 0 if item.concept_id in hint_ids else 1)
    return candidates[:limit]


def _mark_cross_concept_evidences(
    evidences: list[EvidenceItem],
    expanded: list[RelatedConcept],
    original_ids: set[int],
    original_evidences: list[EvidenceItem],
) -> list[EvidenceItem]:
    """给扩展证据打上可区分的标记，同时保留其真实 `concept_id` 归属。

    - 只接受属于 `expanded` 概念的证据；
    - 排除原始概念自己的证据（防止后端越界返回被当成跨概念）；
    - 与原始证据 / 彼此之间按 evidence id 去重；
    - `related_via` 记录扩展依据，`source_concept_id` 记录发起这一跳的原概念。
    """
    if not expanded:
        return []
    by_id = {item.concept_id: item for item in expanded}
    seen_ids = {item.id for item in original_evidences}
    marked: list[EvidenceItem] = []
    for evidence in evidences:
        if evidence.concept_id in original_ids:
            continue
        related = by_id.get(evidence.concept_id)
        if related is None or evidence.id in seen_ids:
            continue
        seen_ids.add(evidence.id)
        label = related.concept_name or str(related.concept_id)
        marked.append(
            evidence.model_copy(
                update={
                    "is_cross_concept": True,
                    "source_concept_id": related.source_concept_id,
                    "related_via": f"{related.relation_type}:{label}",
                }
            )
        )
    return marked


def _retriever_selection(retriever, settings: Settings) -> tuple[str, bool, str | None]:
    """把「用了什么检索源、是否降级、为什么」变成结构化事实。

    - auto 无凭据 → Fixture，reason=no_credentials；
    - auto 有凭据但后端失败 → FallbackRetriever.degraded，reason=backend_unavailable；
    - 显式 fixture → 不叫降级（这是调用方明确选择），但仍可从 resolvedSource 看出。
    """
    mode = settings.retriever_mode.strip().lower()
    if isinstance(retriever, FallbackRetriever):
        if retriever.degraded:
            return mode, True, "backend_unavailable"
        return mode, False, None
    if mode == "auto" and retriever.name.startswith("fixture:"):
        return mode, True, "no_credentials"
    return mode, False, None


def _apply_session_policy(state: AgentState, plan: RetrievalPlan) -> None:
    """按 Policy 决定 Session 的哪些部分进入 Prompt。

    ContextBuilder 读的是 `state.session.goal / current_focus`，这里不改 Builder，
    而是把「不该注入」的字段在 state 上置空。`id` 保留，摘要与溯源不受影响；
    `history` 属于 Conversation 范围，仍按原有逻辑注入，不受 Session 决策影响。

    典型场景（D4-2C 缺口）：SessionResolver 规则 3 把新话题挂到了旧 Session，
    两边概念不相交。此时旧 goal/focus 会误导模型，必须挡在这一层。
    """
    session = state.session
    if session is None:
        return
    if plan.session.inject_goal and plan.session.inject_current_focus:
        return
    state.session = session.model_copy(
        update={
            "goal": session.goal if plan.session.inject_goal else None,
            "current_focus": (
                session.current_focus if plan.session.inject_current_focus else None
            ),
        }
    )


def _provided_labels(prepared: PreparedRun) -> list[str]:
    """当前请求实际注入的合法来源。

    每次调用都从这一个 `prepared` 的 context / knowledge 计算，不读缓存，
    因此不会跨请求、跨用户或跨 Space 复用上一轮的来源集合。
    """
    return provided_labels_from_context(prepared.context, prepared.state.knowledge)
