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
from dataclasses import dataclass
from typing import AsyncIterator

from app.analysis import LearningAnalyzer
from app.citations import (
    CitationReport,
    provided_labels_from_context,
    validate_citations,
)
from app.config import Settings
from app.context.builder import BuiltContext, ContextBuilder
from app.llm.base import LLMAdapter, LLMError
from app.retriever import build_retriever, detect_concepts
from app.retriever.base import (
    KnowledgeQuery,
    KnowledgeResult,
    RetrievalError,
)
from app.retriever.knowledge import KnowledgeSearchService
from app.retriever.routing import FallbackRetriever
from app.schemas import AnalyzeRequest, AnalyzeResult, ChatRequest
from app.state.agent_state import AgentState, DetectedConcept
from app.trace import RunTrace, TraceEventType
from app.tutor import Tutor

log = logging.getLogger(__name__)

# Evidence 是「按需召回」的（架构文档第十二节）：只在用户质疑判断、要求解释依据
# 或要复盘时才去捞。这串词是规则版的触发条件，接 LLM 后由模型判断意图。
_EVIDENCE_TRIGGERS = (
    "为什么",
    "凭什么",
    "你怎么知道",
    "依据",
    "证据",
    "你确定",
    "确定吗",
    "搞错",
    "说错",
    "不对吧",
    "复盘",
    "回顾",
)


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
    | `done`  | `onComplete()` |
    | `error` | `onError(code, message)` |

    所以 `error` 的 data 里必须带 **int 类型的 code** —— `onError` 的签名收的是 int，
    只给 message 的翻译层只能自己编一个常量，等于把错误分类丢在内网这一跳上。
    取值约定见 `_ERROR_CODE_*`。
    """

    event: str  # meta | delta | done | error
    data: dict[str, object]


# error 事件的 code 取值。直接用 HTTP 语义，因为这一跳无论怎么实现都是 HTTP，
# 而且后端 message.error 的 code 字段本来就是一个 int（ChatService 里失败一侧用的就是 500/503）。
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

    @property
    def trace(self) -> RunTrace:
        assert self.state.trace is not None
        return self.state.trace


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        llm: LLMAdapter,
        builder: ContextBuilder | None = None,
        knowledge_search: KnowledgeSearchService | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._knowledge_search = knowledge_search
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
                },
            )

            async for delta in self.generate(prepared):
                yield StreamEvent("delta", {"delta": delta})

            # 只记录、不改变已经产生的回答：当前 SSE 契约没有「引用校验失败」
            # 这个事件，直接从流里截断会破坏前端体验。真正的硬失败仍由评估集
            # 与 trace 告警承担。
            self.validate_citations(prepared)

            trace.record(
                TraceEventType.RUN_END,
                status="COMPLETED",
                responseChars=len(prepared.state.response),
            )
            yield StreamEvent(
                "done",
                {
                    "runId": request.run_id,
                    "detectedConcepts": prepared.state.concept_names,
                    "estimatedTokens": prepared.context.estimated_tokens,
                    "elapsedMs": trace.elapsed_ms,
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

    async def generate(self, prepared: PreparedRun) -> AsyncIterator[str]:
        """把一次已组装好的上下文交给模型，逐个产出增量。

        单独抽出来是为了让离线评估能复用同一条生成链路：评估需要拿到完整
        回答后做引用校验，而不想为了复用 `stream()` 再伪造一套 SSE。
        """
        async for delta in self._tutor.stream(prepared.context):
            prepared.state.response += delta
            yield delta

    def validate_citations(self, prepared: PreparedRun) -> CitationReport:
        """校验本轮回答里的来源标注，并把结果写进 trace。

        校验本身失败（比如某些异常实现返回了非字符串）不应影响已经生成的回答，
        所以入口兜住异常，只记录一条带 error 的 trace。
        """
        try:
            labels = provided_labels_from_context(prepared.context, prepared.state.knowledge)
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

    async def analyze(self, request: AnalyzeRequest) -> AnalyzeResult:
        """从一轮已经完成的对话里抽取学习候选。

        与 `stream` 的关系是**前后脚**：后端在助手回复落库、SSE 结束后调用这里，
        拿到结果再写 Evidence / Understanding / Memory。它不产生用户可见的流，
        失败时由调用方决定是否退回本地规则（而不是把错误展示给用户）。
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
            return await self._analyzer.analyze(request, trace)
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
                knowledge_search=self._knowledge_search,
            )
        try:
            await self._retrieve(state, retriever)
            state.context = self._builder.build(state)
            trace.record(
                TraceEventType.CONTEXT_BUILT,
                estimatedTokens=state.context.estimated_tokens,
                droppedSections=state.context.dropped_sections,
                warnings=state.context.warnings,
            )
        finally:
            if owned_retriever:
                await retriever.aclose()

        return PreparedRun(
            state=state,
            context=state.context,
            retriever_name=retriever.name,
            degraded=isinstance(retriever, FallbackRetriever) and retriever.degraded,
        )

    # ---------- 检索 ----------

    async def _retrieve(self, state: AgentState, retriever) -> None:
        query = state.user_message or ""

        concepts = await retriever.list_concepts(state.space_id)
        trace_detail: dict[str, object] = {"concepts_available": len(concepts)}

        detected = detect_concepts(query, state.history, concepts)
        state.detected_concepts = [
            DetectedConcept(concept=concept, score=score, matched_on=matched_on)
            for concept, score, matched_on in detected
        ]
        state.trace.record(
            TraceEventType.CONCEPT_DETECT,
            detected=[item.concept.label for item in state.detected_concepts],
            matchedOn=[item.matched_on for item in state.detected_concepts],
            **trace_detail,
        )

        concept_ids = state.concept_ids
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

        wants_evidence = bool(concept_ids) and _wants_evidence(query)
        tasks: list = [self._safe_knowledge(retriever, knowledge_query)]
        if concept_ids:
            tasks.extend(
                [
                    retriever.get_understandings(state.user_id, state.space_id, concept_ids),
                    retriever.get_memories(state.user_id, state.space_id, concept_ids, query),
                ]
            )
        if wants_evidence:
            tasks.append(
                retriever.get_evidences(
                    state.user_id, concept_ids, self._settings.evidence_top_k
                )
            )

        results = await asyncio.gather(*tasks)
        knowledge_result: KnowledgeResult = results[0]
        state.knowledge = knowledge_result.chunks
        state.knowledge_status = knowledge_result.status.value
        state.knowledge_warning = knowledge_result.warning

        if concept_ids:
            state.understandings = results[1]
            state.memories = results[2]
            if wants_evidence:
                state.evidences = results[3]

        state.trace.record(
            TraceEventType.RETRIEVE,
            source=retriever.name,
            conceptIds=concept_ids,
            understandings=[item.concept_name for item in state.understandings],
            memories=len(state.memories),
            knowledge=len(state.knowledge),
            knowledgeStatus=state.knowledge_status,
            knowledgeCandidates=knowledge_result.candidates,
            evidences=len(state.evidences),
            evidenceRequested=wants_evidence,
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

    async def aclose(self) -> None:
        await self._llm.aclose()


def _wants_evidence(query: str) -> bool:
    return any(trigger in query for trigger in _EVIDENCE_TRIGGERS)
