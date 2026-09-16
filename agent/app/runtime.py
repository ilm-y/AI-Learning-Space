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

链路下游的「异步学习分析」（Evidence 抽取 → Understanding 更新 → Memory 收敛）
**不在本轮范围内**，会在 `AgentState.pending_tasks` 里如实标出来 ——
现在这部分仍由后端 `LearningAnalysisService` 的规则版分析器顶着。

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

from app.config import Settings
from app.context.builder import BuiltContext, ContextBuilder
from app.llm.base import LLMAdapter, LLMError
from app.retriever import build_retriever, detect_concepts
from app.retriever.base import RetrievalError
from app.retriever.routing import FallbackRetriever
from app.schemas import ChatRequest
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
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._builder = builder or ContextBuilder(
            max_context_tokens=settings.max_context_tokens,
            recent_history_limit=settings.recent_history_limit,
        )
        self._tutor = Tutor(llm)

    @property
    def llm_name(self) -> str:
        return self._llm.name

    # ---------- 同步路径 ----------

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        trace = RunTrace(run_id=request.run_id)
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

            async for delta in self._tutor.stream(prepared.context):
                prepared.state.response += delta
                yield StreamEvent("delta", {"delta": delta})

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

    async def prepare(self, request: ChatRequest, trace: RunTrace | None = None) -> PreparedRun:
        """检索 + 组装，不调用模型。preview 接口直接复用它。"""
        trace = trace or RunTrace(run_id=request.run_id)
        state = AgentState.from_request(request, trace)
        trace.record(
            TraceEventType.RUN_START,
            userId=request.user_id,
            conversationId=request.conversation_id,
            spaceId=request.learning_space_id,
            messageChars=len(request.user_message or ""),
        )

        retriever = build_retriever(self._settings, request.auth_token)
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
        if not concept_ids:
            # 没命中概念不是错误：正常聊天、问新知识点都属于这一类。
            # 检索一律跳过，省掉三次无意义的往返。
            return

        wants_evidence = _wants_evidence(query)
        tasks = [
            retriever.get_understandings(state.user_id, state.space_id, concept_ids),
            retriever.get_memories(state.user_id, state.space_id, concept_ids, query),
            retriever.get_knowledge(state.space_id, concept_ids, query),
        ]
        if wants_evidence:
            tasks.append(retriever.get_evidences(state.user_id, concept_ids, self._settings.evidence_top_k))

        results = await asyncio.gather(*tasks)
        state.understandings = results[0]
        state.memories = results[1]
        state.knowledge = results[2]
        if wants_evidence:
            state.evidences = results[3]

        state.trace.record(
            TraceEventType.RETRIEVE,
            source=retriever.name,
            conceptIds=concept_ids,
            understandings=[item.concept_name for item in state.understandings],
            memories=len(state.memories),
            knowledge=len(state.knowledge),
            evidences=len(state.evidences),
            evidenceRequested=wants_evidence,
        )

    async def aclose(self) -> None:
        await self._llm.aclose()


def _wants_evidence(query: str) -> bool:
    return any(trigger in query for trigger in _EVIDENCE_TRIGGERS)
