"""与后端对齐的数据契约。

这些模型是 Backend 与 Agent 之间的**唯一**数据边界，字段命名跟随 Java record 的
Jackson 序列化结果（camelCase），取值跟随 `entity/enums` 里的枚举常量名。

Backend 侧的对应物：
- `ChatRequest`      ←→ `dto/agent/ChatAgentRequest` + `ChatHistoryMessage`
- `Concept`          ←→ `dto/concept/ConceptResponse`
- `Understanding`    ←→ `dto/understanding/UnderstandingResponse`
- `MemoryItem`       ←→ `dto/memory/MemoryResponse`
- `EvidenceItem`     ←→ `dto/understanding/EvidenceResponse`
- `ApiResponse[T]`   ←→ `common/ApiResponse`

Agent **不重复实现**后端的写入语义。这里全是只读模型：本轮 Agent 只负责
「检索 → 组装 → 教学」，Evidence / Understanding 的写入仍由后端完成。
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


class EvidenceCorrectness(str, Enum):
    CORRECT = "CORRECT"
    INCORRECT = "INCORRECT"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


# ===================== 请求 =====================


class HistoryMessage(CamelModel):
    role: MessageRole
    content: str

    def is_user(self) -> bool:
        return self.role is MessageRole.USER


class ChatRequest(CamelModel):
    """Backend → Agent 的一次对话请求。

    与 `ChatAgentRequest` 对齐，另外多一个 `auth_token`：Agent 要用它回调后端的
    读接口（`/api/understandings`、`/api/memories/search`…）。这些接口按登录用户鉴权，
    所以必须把用户自己的 JWT 透传过来；没传就退回 Fixture 数据。
    """

    run_id: int
    user_id: int
    conversation_id: int
    learning_space_id: int
    user_message: str
    history: list[HistoryMessage] = Field(default_factory=list)
    auth_token: str | None = None


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
    learning_space_id: int | None = None
    concept_id: int | None = None
    memory_type: MemoryType = MemoryType.LEARNING
    path: str | None = None
    content: str
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: float | None = None


class EvidenceItem(CamelModel):
    id: int
    concept_id: int
    understanding_id: int | None = None
    observation: str
    correctness: EvidenceCorrectness = EvidenceCorrectness.UNKNOWN
    reasoning_quality: float | None = None
    specificity: float | None = None
    ai_confidence: float | None = None
    created_at: datetime | None = None


class KnowledgeChunk(CamelModel):
    """知识片段。

    第一版没有文档解析与向量检索，只有 Fixture 里的样例数据；
    字段与 `document_chunk` 对齐，接 RAG 时不用改上面的层。

    `concept_id` 是 Agent 侧补的：`document_chunk` 表里没有这一列，
    第一版靠它把片段挂到 Concept 上做筛选；接向量检索后由相似度与 metadata 取代。
    """

    id: int
    document_id: int | None = None
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
