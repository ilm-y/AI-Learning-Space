"""AgentState —— 一次 Agent 运行的运行时状态。

对应 `agent-architecture.md` 第三十八、三十九节：

> `AgentState` 是运行时状态，不是数据库里的长期实体。

所以这个类**刻意不落库、不跨请求复用**：一次请求构造一个，请求结束即丢弃。
跨对话的长期连续性由后端的 Understanding / Memory / Evidence 承担，
Agent 每次运行通过 Retriever 把它们读进来 —— 这就是
「checkpointer 管 thread-scoped state，store 管跨 thread 信息」在本项目里的落地方式。

字段与设计文档第三十八节给的结构一一对应，多出来的 `context` / `trace` / `response`
是一次运行内的中间产物，只用于调试与观测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.schemas import (
    ChatRequest,
    Concept,
    EvidenceItem,
    HistoryMessage,
    KnowledgeChunk,
    MemoryItem,
    Understanding,
)
from app.trace import RunTrace

if TYPE_CHECKING:  # 避免与 app.context.builder 形成循环导入
    from app.context.builder import BuiltContext


@dataclass(slots=True)
class DetectedConcept:
    """用户这句话命中的 Concept，以及命中的依据。

    `matched_on` 是给调试用的：用户看不出「为什么这次召回了 TLB」，
    但排查召回问题时需要它。
    """

    concept: Concept
    score: float
    matched_on: str


@dataclass
class AgentState:
    # ---------- 请求身份 ----------
    run_id: int
    user_id: int
    conversation_id: int
    space_id: int

    # ---------- 当前输入与对话 ----------
    user_message: str
    history: list[HistoryMessage] = field(default_factory=list)

    # ---------- 检索结果 ----------
    detected_concepts: list[DetectedConcept] = field(default_factory=list)
    understandings: list[Understanding] = field(default_factory=list)
    memories: list[MemoryItem] = field(default_factory=list)
    knowledge: list[KnowledgeChunk] = field(default_factory=list)
    evidences: list[EvidenceItem] = field(default_factory=list)

    # ---------- 组装与产出 ----------
    context: "BuiltContext | None" = None
    response: str = ""

    # ---------- 观测 ----------
    trace: RunTrace | None = None
    pending_tasks: list[str] = field(default_factory=list)

    @classmethod
    def from_request(cls, request: ChatRequest, trace: RunTrace) -> "AgentState":
        return cls(
            run_id=request.run_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            space_id=request.learning_space_id,
            user_message=request.user_message,
            history=list(request.history),
            trace=trace,
            # 异步学习分析还没有接入 Agent，先如实记下来，避免误以为已经做了闭环
            pending_tasks=["evidence_extraction", "understanding_update", "memory_consolidation"],
        )

    @property
    def concept_ids(self) -> list[int]:
        return [item.concept.id for item in self.detected_concepts]

    @property
    def concept_names(self) -> list[str]:
        return [item.concept.label for item in self.detected_concepts]

    def summary(self) -> dict[str, object]:
        """一次 run 的输入/输出摘要，对应 `agent_run` 的 input_summary / output_summary。"""
        return {
            "runId": self.run_id,
            "userId": self.user_id,
            "conversationId": self.conversation_id,
            "spaceId": self.space_id,
            "userMessage": self.user_message,
            "detectedConcepts": self.concept_names,
            "retrieved": {
                "understandings": len(self.understandings),
                "memories": len(self.memories),
                "knowledge": len(self.knowledge),
                "evidences": len(self.evidences),
                "history": len(self.history),
            },
            "responseChars": len(self.response),
            "pendingTasks": self.pending_tasks,
        }
