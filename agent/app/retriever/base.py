"""Retriever —— 把「跨对话的用户认知」读进本次运行。

对应 `agent-architecture.md` 第八节的三条检索线（Knowledge / Understanding / Memory）
加上按需召回的 Evidence：

```text
Concept 识别（规则版命中名称）
        ↓
Understanding  Retrieval   ← 我现在该怎么讲
Memory         Retrieval   ← 这个用户长期值得记住什么
Knowledge      Retrieval   ← 我该参考用户上传的哪些资料（白名单 + 向量检索）
Evidence       Retrieval   ← 为什么系统这样判断（按需，不要每次都灌）
```

三条线都是**失败可降级**的：检索不到不是错误，只是这次个性化弱一些。
所以实现里大量使用「返回空列表」而不是抛异常 —— 只有真正连不上后端时才会向上报。
Knowledge 更严格：权限或依赖失败时返回**带 warning 的空结果**，绝不回退到
没有权限过滤的向量搜索，也不回退 Fixture 样例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from app.schemas import (
    Concept,
    EvidenceItem,
    KnowledgeChunk,
    MemoryItem,
    RelatedConcept,
    Understanding,
    UnderstandingRevision,
)


class RetrievalError(RuntimeError):
    """检索源整体不可用（后端连不上 / JWT 失效 / 业务错误）。

    「这个 Concept 没有快照」不是错误，不该抛这个 —— 那属于正常返回空。
    """


class KnowledgeStatus(str, Enum):
    """一次知识检索的结果状态，进 trace / 日志 / 上下文 warning。

    这里刻意把「没有可用文档」「没有命中」「被阈值过滤」「依赖失败」分开：
    它们对用户和排查者的含义完全不同，合并成一个「空列表」会让故障不可见。
    """

    SUCCESS = "SUCCESS"
    NO_VALID_DOCUMENTS = "NO_VALID_DOCUMENTS"
    NO_MATCH = "NO_MATCH"
    THRESHOLD_FILTERED = "THRESHOLD_FILTERED"
    DEGRADED = "DEGRADED"
    NOT_RETRIEVED = "NOT_RETRIEVED"


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    """知识检索需要的全部上下文。

    `user_id` / `space_id` 只来自后端已经认证过的请求，绝不从自然语言推断；
    它们同时用于后端文档白名单与 Qdrant payload 过滤。
    """

    user_id: int
    space_id: int
    question: str
    concept_ids: tuple[int, ...] = ()
    top_k: int = 5
    min_score: float = 0.35

    def __post_init__(self) -> None:
        if self.user_id <= 0 or self.space_id <= 0:
            raise ValueError("知识检索必须携带有效的 userId / spaceId")
        if self.top_k <= 0:
            raise ValueError("知识检索 top_k 必须为正数")


@dataclass(slots=True)
class KnowledgeResult:
    """知识检索结果。chunks 为空时 status 必须说明原因。"""

    chunks: list[KnowledgeChunk] = field(default_factory=list)
    status: KnowledgeStatus = KnowledgeStatus.NO_MATCH
    warning: str | None = None
    candidates: int = 0

    @classmethod
    def degraded(cls, warning: str) -> "KnowledgeResult":
        return cls(status=KnowledgeStatus.DEGRADED, warning=warning)


class RelatedConceptStatus(str, Enum):
    """一次相关概念查询的结果状态。

    **不能把 FAILED 当成 EMPTY**：接口失败时如果只返回空列表，上层就会把
    “没查到关系”和“关系查询坏了”混为一谈，用户得到的是一次静默的弱化召回。
    """

    OK = "OK"
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(slots=True)
class RelatedConceptResult:
    """相关概念查询结果。

    items 是“另一端概念”的候选；status 说明查询本身是否完整。
    warning 在 FAILED / PARTIAL 时给出可观测的原因，会进 trace 与 meta warnings。
    """

    items: list[RelatedConcept] = field(default_factory=list)
    status: RelatedConceptStatus = RelatedConceptStatus.EMPTY
    warning: str | None = None


@runtime_checkable
class Retriever(Protocol):
    @property
    def name(self) -> str:
        """检索源标识，进日志与 /health。"""

    async def list_concepts(self, space_id: int) -> list[Concept]:
        """空间下的全部 Concept —— 同时是概念识别的候选集。"""
        ...

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        """用户在这些 Concept 上的当前认知快照。"""
        ...

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        """与当前话题相关的长期 Memory。只取 ACTIVE —— CANDIDATE 还没被用户确认。"""
        ...

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        """参考知识。

        无论本轮是否命中 Concept 都会调用：概念命中只决定 U / M / E，
        不应该连带决定「要不要看用户上传的资料」。

        权限失败、依赖失败必须 fail closed，返回带 warning 的空结果，
        不得抛成普通对话 5xx，也不得退化成无过滤的向量搜索。
        """
        ...

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        """判定依据。按需召回，默认条数很小 —— 不是每次回答都需要它。"""
        ...

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        """查询识别概念的一跳相关概念，供 Evidence 受控扩展使用。

        只在 Evidence 检索被触发时调用；失败必须返回 FAILED / PARTIAL 与 warning，
        不能伪装成“没有相关概念”。结果中的 `source_concept_id` 由具体实现补上，
        表示这一跳是从哪个原概念出发的。
        """
        ...

    async def get_revisions(
        self, user_id: int, concept_ids: list[int]
    ) -> dict[int, list[UnderstandingRevision]]:
        """理解快照的不可变版本历史，按 conceptId 分组。

        只有显式复盘才调用；它用于找到「误区何时出现、何时被纠正」以及
        每次变化的 triggerEvidenceId，而不是把所有版本无差别塞进上下文。
        """
        ...

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ) -> list[EvidenceItem]:
        """按 id 精确读取证据及其来源。

        复盘时用 Revision 的 trigger/reversal 关联直接回查原始证据，
        不再按 createdAt 取最近 N 条。
        """
        ...

    async def aclose(self) -> None:
        """释放底层资源。"""
        ...
