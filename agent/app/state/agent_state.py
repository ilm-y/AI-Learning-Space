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
    LearningTrajectory,
    MemoryItem,
    RelatedConcept,
    SessionContext,
    Understanding,
)
from app.trace import RunTrace

if TYPE_CHECKING:  # 避免与 app.context.builder / policy 形成循环导入
    from app.context.builder import BuiltContext
    from app.context.policy import RetrievalPlan


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

    # ---------- 短期工作上下文 ----------
    # 这条对话归属的学习活动（后端 session-design.md §一.4）。为 None 是常态：
    # Session 懒创建，一条刚建出来的对话在第一条有效学习消息之前没有任何归属，
    # 而第一条消息本身恰好就带着 None 出发。渲染层据此跳过该区块。
    session: SessionContext | None = None

    # ---------- 检索结果 ----------
    detected_concepts: list[DetectedConcept] = field(default_factory=list)
    # ---------- E2 结构化概念解析（Resolver）----------
    # `resolver_current_concept_ids` 已经过服务端校验，可以进入 concept_ids；
    # `resolver_related_concept_ids` 只是 E1 关系扩展的排序提示，**不是**检索范围。
    # NOT_CALLED / SUCCESS / DEGRADED / FAILED：DEGRADED 不能当成「用户没有历史」。
    resolver_status: str = "NOT_CALLED"
    resolver_intent: str = "other"
    resolver_call_reason: str = ""
    resolver_warning: str | None = None
    resolver_current_concept_ids: list[int] = field(default_factory=list)
    resolver_related_concept_ids: list[int] = field(default_factory=list)
    resolver_needs_history_reference: bool = False
    resolver_prompt_tokens: int = 0
    resolver_completion_tokens: int = 0
    resolver_token_source: str = "estimated"
    resolver_elapsed_ms: int = 0
    resolver_dropped: list[str] = field(default_factory=list)
    understandings: list[Understanding] = field(default_factory=list)
    # 本轮学习认知检索状态：NOT_QUERIED / SUCCESS / EMPTY / DEGRADED。
    # 必须与 `understandings` 分开保存 —— 空列表既可能是「确实没有快照」，
    # 也可能是「没有发起检索」或「查询失败」，三者的上下文措辞完全不同。
    understanding_status: str = "NOT_QUERIED"
    understanding_warning: str | None = None
    memories: list[MemoryItem] = field(default_factory=list)
    # 本轮记忆检索状态：NOT_QUERIED / SUCCESS / EMPTY / DEGRADED。
    # `memory_detail` 保留后端细分状态（OK / DEGRADED_VECTOR / UNAVAILABLE / ...），
    # 只用于 trace 与 preview，不进入 Prompt 正文。
    memory_status: str = "NOT_QUERIED"
    memory_warning: str | None = None
    memory_detail: str | None = None
    knowledge: list[KnowledgeChunk] = field(default_factory=list)
    # 知识检索的失败/空结果原因。它是「这次为什么没有用资料」的唯一事实，
    # 会进 trace、meta warnings 与 context preview；不要把失败伪装成一次成功空检索。
    knowledge_status: str = "NOT_RETRIEVED"
    knowledge_warning: str | None = None
    evidences: list[EvidenceItem] = field(default_factory=list)
    # 本轮 Evidence 检索状态：NOT_RETRIEVED / SUCCESS / EMPTY / DEGRADED。
    # DEGRADED 表示查询失败或结果不完整，**不能**当成「用户没有历史证据」；
    # ContextBuilder 会据此输出明确提示。
    evidence_status: str = "NOT_RETRIEVED"
    evidence_warning: str | None = None
    # ---------- 受控一跳跨概念扩展（仅 Evidence 检索触发时填充）----------
    # related_concepts 是后端返回的全部候选；expanded_concepts 是最终被选中、
    # 真正去查 Evidence 的最多 N 个概念（已排除原始识别概念并去重）。
    related_concepts: list[RelatedConcept] = field(default_factory=list)
    expanded_concepts: list[RelatedConcept] = field(default_factory=list)
    # 跨概念证据也进入 evidences 供 Guard/渲染使用，但单独保留一份便于区分与观测。
    cross_concept_evidences: list[EvidenceItem] = field(default_factory=list)
    # NOT_RETRIEVED / OK / EMPTY / PARTIAL / FAILED。失败不能伪装成 EMPTY。
    related_concepts_status: str = "NOT_RETRIEVED"
    related_concepts_warning: str | None = None
    # 跨概念 Evidence 查询复用同一套三态语义，单独保留状态便于观测与上下文提示。
    cross_concept_evidence_status: str = "NOT_RETRIEVED"
    cross_concept_evidence_warning: str | None = None
    # 仅显式复盘时填充：真实 Revision 历史 + 由 trigger/reversal 关联精确找回的 Evidence。
    trajectories: list[LearningTrajectory] = field(default_factory=list)
    # 历史轨迹检索状态：NOT_QUERIED（本轮没复盘）/ SUCCESS / EMPTY / DEGRADED。
    # DEGRADED 绝不能解释成「用户过去没有误区、没有变化」。
    revision_status: str = "NOT_QUERIED"
    revision_warning: str | None = None
    # 用户这句话是否是显式个人复盘（由 Policy 的 review_required 判定，与是否检索到概念无关）。
    # Runtime 只对这类回答做「生成后、推送前」的证据校验：普通知识问题仍即时流式，
    # 不能被缓冲。它也是 Current-only 场景的判断依据 —— 没有轨迹但要约束回答。
    history_review_requested: bool = False
    # ---------- D4-2 检索策略 ----------
    # 本轮「查什么、为什么查」的结构化决策（意图 / Session 注入 / 各数据源 need）。
    # Runtime 按它执行检索；ContextBuilder 不消费它。保存在 state 上是为了让
    # trace、preview 与后续评估复用同一份决策，而不是从调用结果反推。
    retrieval_plan: "RetrievalPlan | None" = None

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
            session=request.session,
            trace=trace,
            # 学习分析是回答结束后独立的一次 POST /agent/analyze，不属于本次 chat run。
            # 不要把「Evidence 抽取 / Understanding 更新 / Memory 收敛」固定塞成待办：
            # 那会让每一次 chat 的 summary 都永远显示「未完成」，等于把另一条链路的
            # 状态错误地写进了这一条。
            pending_tasks=[],
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
            "sessionId": self.session.id if self.session else None,
            "userMessage": self.user_message,
            "detectedConcepts": self.concept_names,
            "resolverStatus": self.resolver_status,
            "retrieved": {
                "understandings": len(self.understandings),
                "understandingStatus": self.understanding_status,
                "memories": len(self.memories),
                "memoryStatus": self.memory_status,
                "knowledge": len(self.knowledge),
                "evidences": len(self.evidences),
                "history": len(self.history),
                "revisionStatus": self.revision_status,
            },
            "responseChars": len(self.response),
            "pendingTasks": self.pending_tasks,
        }
