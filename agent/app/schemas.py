"""与后端对齐的数据契约。

这些模型是 Backend 与 Agent 之间的**唯一**数据边界，字段命名跟随 Java record 的
Jackson 序列化结果（camelCase），取值跟随 `entity/enums` 里的枚举常量名。

Backend 侧的对应物：
- `ChatRequest`      ←→ `dto/agent/ChatAgentRequest` + `ChatHistoryMessage`
- `AnalyzeRequest`   ←→ `dto/agent/AnalyzeAgentRequest`
- `AnalyzeResult`    ←→ `dto/agent/AnalyzeAgentResult`（及四个嵌套 record）
- `Concept`          ←→ `dto/concept/ConceptResponse`
- `Understanding`    ←→ `dto/understanding/UnderstandingResponse`
- `MemoryItem`       ←→ `dto/memory/MemoryResponse`
- `EvidenceItem`     ←→ `dto/understanding/EvidenceResponse`
- `ApiResponse[T]`   ←→ `common/ApiResponse`

Agent **不重复实现**后端的写入语义：Chat 链路只读，Analyze 链路只生产候选。
Evidence 去重、Understanding 合并、Memory 作用域与生命周期都由后端决定，
Agent 的输出必须能在不依赖任何写入顺序的前提下被校验。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class CamelModel(BaseModel):
    """接受 camelCase（后端）与 snake_case（Python 侧手写）两种写法。"""

    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="ignore")


# ===================== 枚举（与 Java enum 常量名一一对应）=====================


class MessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"
    TOOL = "TOOL"


class UnderstandingStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    EXPOSED = "EXPOSED"
    PARTIAL = "PARTIAL"
    DEMONSTRATED = "DEMONSTRATED"
    MISCONCEPTION = "MISCONCEPTION"


class MisconceptionStatus(str, Enum):
    ACTIVE = "ACTIVE"  # 当前存在
    PENDING = "PENDING"  # 已出现反向证据，可能正在解除


class MemoryType(str, Enum):
    USER = "USER"
    LEARNING = "LEARNING"
    PREFERENCE = "PREFERENCE"


class MemoryStatus(str, Enum):
    CANDIDATE = "CANDIDATE"  # Agent 推断，待用户确认
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MemoryScopeType(str, Enum):
    """Memory 的作用域（后端 scope_type）。

    决定一条记忆在什么上下文里可见：GLOBAL 对任意空间可见，SPACE 只在本空间，
    CONCEPT 只在本空间的某个知识点下。后端已经按这个规则过滤过，
    Agent 侧只用来在 prompt 里标注「这条记忆是什么级别的事实」。
    """

    GLOBAL = "GLOBAL"
    SPACE = "SPACE"
    CONCEPT = "CONCEPT"


class EvidenceCorrectness(str, Enum):
    CORRECT = "CORRECT"
    INCORRECT = "INCORRECT"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class EvidenceType(str, Enum):
    """证据的「来源形态」，与 correctness（判断是什么）正交。

    取值必须与后端 `enums/EvidenceType.java` 一致：它参与 dedupe_key 的计算，
    改一个字母等于让历史证据全部换一个身份。
    """

    USER_EXPLANATION = "USER_EXPLANATION"
    OBSERVATION = "OBSERVATION"
    TEST_RESULT = "TEST_RESULT"
    USER_CORRECTION = "USER_CORRECTION"
    HIGHLIGHT_SIGNAL = "HIGHLIGHT_SIGNAL"


class QuestionType(str, Enum):
    SINGLE_CHOICE = "SINGLE_CHOICE"
    TRUE_FALSE = "TRUE_FALSE"
    SHORT_ANSWER = "SHORT_ANSWER"


class VerificationAction(str, Enum):
    """主动验证策略。只有 ASK_NOW 会在对话里插入 Test Card。"""

    SKIP = "SKIP"
    ASK_NOW = "ASK_NOW"
    ASK_LATER = "ASK_LATER"


class VerificationTrigger(str, Enum):
    NEW_CONCEPT = "NEW_CONCEPT"
    CONCEPT_CONFLICT = "CONCEPT_CONFLICT"
    REPEATED_ERROR = "REPEATED_ERROR"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    OVERCONFIDENCE = "OVERCONFIDENCE"
    LONG_UNVERIFIED = "LONG_UNVERIFIED"


class MemoryReconcileAction(str, Enum):
    """Memory Reconciler 的三档决策（PRD §13）。"""

    ADD = "ADD"
    UPDATE = "UPDATE"
    IGNORE = "IGNORE"


# ===================== 请求 =====================


class HistoryMessage(CamelModel):
    role: MessageRole
    content: str

    def is_user(self) -> bool:
        return self.role is MessageRole.USER


class SessionContext(CamelModel):
    """本阶段学习活动的短期工作上下文（后端 `session-design.md` §一.4）。

    只有 `goal`（这一阶段最终要解决什么）和 `current_focus`（此刻卡在哪）会进 prompt ——
    `id` / `title` 是给日志和溯源用的，不渲染。

    `current_focus` 会随对话推进变化，所以同一个 Session 在不同轮次渲染出的区块可能不同。
    """

    id: int
    title: str | None = None
    goal: str | None = None
    current_focus: str | None = None


class ChatRequest(CamelModel):
    """Backend → Agent 的一次对话请求。

    与 `ChatAgentRequest` 对齐，另外多一个 `auth_token`：Agent 要用它回调后端的
    读接口（`/api/understandings`、`/api/memories/search`…）。这些接口按登录用户鉴权，
    所以必须把用户自己的 JWT 透传过来；没传就退回 Fixture 数据。
    """

    run_id: int
    # 跨进程关联键：后端生成的 32 位十六进制，两跳（后端 → Agent）靠它对日志。
    trace_id: str | None = None
    user_id: int
    conversation_id: int
    learning_space_id: int
    user_message: str
    history: list[HistoryMessage] = Field(default_factory=list)
    # 可为 None —— Session 是懒创建的，一条刚建出来的对话在第一条有效学习消息之前
    # 还没有归属。这是正常状态，不是数据缺失。
    session: SessionContext | None = None
    auth_token: str | None = None


class AnalyzeRequest(CamelModel):
    """Backend → Agent 的一次学习分析请求（`POST /agent/analyze`）。

    与 ChatRequest 的区别：分析发生在回答**之后**，所以多带一条
    `assistant_message`（用于判断这轮对话是否真的产生了学习信号），
    同时带上本轮消息的 `message_id` —— 后端会把 Evidence 的来源指回这条消息。

    Agent 只提出候选（evidence / understanding / memory / verification），
    真正的写入由后端在事务里完成：Agent 不连数据库，也不拥有写入语义。
    """

    run_id: int
    trace_id: str | None = None
    user_id: int
    conversation_id: int
    learning_space_id: int
    # 本轮用户消息的数据库 id。取不到（例如离线调用）时为 None，此时后端不会落 Evidence。
    message_id: int | None = None
    user_message: str
    assistant_message: str | None = None
    history: list[HistoryMessage] = Field(default_factory=list)
    session: SessionContext | None = None
    auth_token: str | None = None


# ===================== 分析结果（Agent → Backend）=====================


class EvidenceCandidate(CamelModel):
    """一条候选证据。后端负责 dedupe（`dedupe_key` UNIQUE）与 append-only 写入。

    `concept_id` 必须来自本轮检索到的 Concept；后端会再校验一次，
    防止模型凭空编一个 id 把证据写到别的知识点上。
    """

    concept_id: int
    evidence_type: EvidenceType = EvidenceType.OBSERVATION
    observation: str
    correctness: EvidenceCorrectness = EvidenceCorrectness.UNKNOWN
    # 三个质量分都在 [0, 1]。模型偶尔会给出 1.2 或 -0.1，解析后统一裁剪而不是整份结果作废。
    reasoning_quality: float | None = None
    specificity: float | None = None
    ai_confidence: float | None = None
    need_follow_up: bool = False
    # 只有 USER_EXPLANATION / USER_CORRECTION 才有意义：用户自己的原话。
    user_explanation: str | None = None


class UnderstandingProposal(CamelModel):
    """对「当前理解」的增量提案。

    这是**增量**，不是整份快照：Agent 只说自己新观察到了什么，
    合并规则（去重、误区生命周期、状态派生）仍由后端
    `UnderstandingService` 负责 —— 那里是唯一真相。
    """

    concept_id: int | None = None
    known_parts: list[str] = Field(default_factory=list)
    uncertain_parts: list[str] = Field(default_factory=list)
    misconceptions: list[str] = Field(default_factory=list)
    latest_user_explanation: str | None = None


class MemoryCandidate(CamelModel):
    """一条 Memory 候选。Agent 只能提出 CANDIDATE，不能直接 ACTIVE。

    `action` 是 Reconciler 的建议，后端仍要按 scope + memory_key 查一次：
    - ADD：没有同身份记忆时新增；已存在则忽略（不覆盖用户确认过的内容）
    - UPDATE：只更新仍是 CANDIDATE 的那条；用户已确认的 ACTIVE 不碰
    - IGNORE：不写
    """

    scope_type: MemoryScopeType = MemoryScopeType.CONCEPT
    learning_space_id: int | None = None
    concept_id: int | None = None
    memory_type: MemoryType = MemoryType.LEARNING
    memory_key: str | None = None
    content: str
    importance: float | None = None
    action: MemoryReconcileAction = MemoryReconcileAction.ADD


class VerificationDecision(CamelModel):
    """要不要插入一次验证，以及插什么。

    `action = SKIP` 时其余字段都可以为空；`ASK_NOW` 必须给出可作答的
    `question_text`（后端在建 TestTask 前会再检查一次）。
    """

    action: VerificationAction = VerificationAction.SKIP
    trigger: VerificationTrigger | None = None
    concept_id: int | None = None
    reason: str | None = None
    question_type: QuestionType | None = None
    question_text: str | None = None
    correct_answer: str | None = None


class AnalyzeResult(CamelModel):
    """一次学习分析的完整结果。字段名与后端 `AnalyzeAgentResult` 对齐。"""

    evidence_candidates: list[EvidenceCandidate] = Field(default_factory=list)
    understanding_proposal: UnderstandingProposal | None = None
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list)
    verification_decision: VerificationDecision = Field(default_factory=VerificationDecision)
    # 分析器版本：进 Evidence.analyzer_version，用于回答「这条证据是哪版逻辑抽出来的」。
    analyzer_version: str = "agent-v1"
    # 命中的概念名称，与 chat 的 detectedConcepts 同义，只用于日志与排查。
    detected_concepts: list[str] = Field(default_factory=list)


# ===================== 领域只读模型 =====================


class Misconception(CamelModel):
    """当前快照里的一个误区条目。

    只有 ACTIVE / PENDING 两种状态会出现在这里：解除即从快照移除，
    `RESOLVED` 不落库。历史通过 Revision 与 Evidence 追溯。
    """

    text: str
    status: MisconceptionStatus = MisconceptionStatus.ACTIVE
    first_detected_at: datetime | None = None
    last_evidence_at: datetime | None = None
    reversal_evidence_ids: list[int] = Field(default_factory=list)


class Concept(CamelModel):
    id: int
    canonical_name: str
    display_name: str | None = None
    description: str | None = None
    difficulty_level: int | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.canonical_name


class Understanding(CamelModel):
    """用户对某个 Concept 的当前认知快照 —— 个性化教学的依据。"""

    id: int
    concept_id: int
    concept_name: str
    summary: str | None = None
    known_parts: list[str] = Field(default_factory=list)
    misconceptions: list[Misconception] = Field(default_factory=list)
    uncertain_parts: list[str] = Field(default_factory=list)
    latest_user_explanation: str | None = None
    status: UnderstandingStatus = UnderstandingStatus.UNKNOWN
    current_revision_id: int | None = None
    updated_at: datetime | None = None

    @property
    def has_content(self) -> bool:
        """UNKNOWN 且什么都没有的快照不往上下文里塞 —— 它不提供任何信息。"""
        return bool(
            self.summary
            or self.known_parts
            or self.misconceptions
            or self.uncertain_parts
            or self.latest_user_explanation
        )


class MemoryItem(CamelModel):
    id: int
    scope_type: MemoryScopeType = MemoryScopeType.GLOBAL
    learning_space_id: int | None = None
    concept_id: int | None = None
    memory_type: MemoryType = MemoryType.LEARNING
    # 同作用域内的记忆标识（后端旧字段名是 path）。仅用于溯源，不参与 prompt 渲染。
    memory_key: str | None = None
    content: str
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: float | None = None
    # 时间用于让模型知道「这条记忆可能早于当前理解」，而不是把它当成本轮事实。
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvidenceSourceItem(CamelModel):
    """Evidence 的原始来源（消息 / 批注 / 测试回答）。"""

    source_type: str
    source_id: int


class EvidenceItem(CamelModel):
    id: int
    concept_id: int
    understanding_id: int | None = None
    evidence_type: EvidenceType = EvidenceType.OBSERVATION
    observation: str
    correctness: EvidenceCorrectness = EvidenceCorrectness.UNKNOWN
    reasoning_quality: float | None = None
    specificity: float | None = None
    ai_confidence: float | None = None
    created_at: datetime | None = None
    sources: list[EvidenceSourceItem] = Field(default_factory=list)


class UnderstandingRevision(CamelModel):
    """理解快照的一次不可变版本。

    只读；字段与后端 `UnderstandingRevisionResponse` 对齐。`trigger_evidence_id`
    是这次变化由哪条证据触发的直接关联，复盘时用它精确回查原始证据。
    """

    id: int
    # 后端 revisions 接口按 concept 查询，响应本身不带该字段；Fixture 需要它分组。
    concept_id: int | None = None
    revision_no: int
    summary: str | None = None
    known_parts: list[str] = Field(default_factory=list)
    misconceptions: list[Misconception] = Field(default_factory=list)
    uncertain_parts: list[str] = Field(default_factory=list)
    user_explanation: str | None = None
    status: UnderstandingStatus = UnderstandingStatus.UNKNOWN
    trigger_evidence_id: int | None = None
    created_at: datetime | None = None


class LearningTrajectory(CamelModel):
    """某个概念在复盘时需要的真实历史轨迹。

    它不代表当前状态：`current_status` / `current_misconceptions` 来自本轮的
    Understanding 快照，`revisions` 与 `evidences` 则是过去的不可变记录。
    """

    concept_id: int
    concept_name: str
    current_status: UnderstandingStatus = UnderstandingStatus.UNKNOWN
    current_misconceptions: list[str] = Field(default_factory=list)
    revisions: list[UnderstandingRevision] = Field(default_factory=list)
    evidences: list[EvidenceItem] = Field(default_factory=list)


class KnowledgeChunk(CamelModel):
    """知识片段。

    真实 RAG 链路会填充文档名、片段序号、文件类型与可选的标题路径，
    让模型引用的来源可被人核对，而不是只看到内部数据库 ID。
    Fixture 数据可以只提供旧字段，渲染层会退化到片段 ID。
    """

    id: int
    document_id: int | None = None
    chunk_index: int | None = None
    document_name: str | None = None
    file_type: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    concept_id: int | None = None
    content: str
    page_number: int | None = None
    token_count: int | None = None
    score: float | None = None


class ApiResponse(CamelModel):
    """后端统一响应体。`code = 0` 才是成功。"""

    code: int = 0
    message: str | None = None
    data: object = None

    def is_ok(self) -> bool:
        return self.code == 0
