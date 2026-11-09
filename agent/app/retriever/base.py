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


class RetrievalStatus(str, Enum):
    """一次检索的「检索阶段」状态（与注入阶段状态分开）。

    - ``NOT_QUERIED``：本轮没有发起这类检索（例如没有识别到概念）；
    - ``EMPTY``：已经查过，且确实没有数据；
    - ``SUCCESS``：查到了数据；
    - ``DEGRADED``：查询失败或结果不完整，**不能**当成 EMPTY。

    这四个值刻意互斥。不要把「未查询」「查询失败」「确实为空」压成同一个空列表 ——
    模型会据此断言用户「从未学过 / 没有记忆」，而事实可能只是这次没查或没查成。
    """

    NOT_QUERIED = "NOT_QUERIED"
    EMPTY = "EMPTY"
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"


@dataclass(slots=True)
class UnderstandingRetrievalResult:
    """学习认知快照的检索结果。

    ``status=DEGRADED`` 表示至少一个 concept 的查询失败或响应无法解析。
    已成功取得的快照仍放在 ``items`` 里（部分成功），但整体不能被当作 EMPTY。
    """

    items: list[Understanding] = field(default_factory=list)
    status: RetrievalStatus = RetrievalStatus.EMPTY
    warning: str | None = None
    failed_concept_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class MemoryRetrievalResult:
    """长期记忆的检索结果。

    ``detail`` 保留后端原始细分状态（OK / DEGRADED_VECTOR / DEGRADED_KEYWORD /
    UNAVAILABLE / BLANK_QUERY），只用于结构化观测；``warning`` 是脱敏后的可读原因，
    可以进入 meta 与 prompt 的状态说明。
    """

    items: list[MemoryItem] = field(default_factory=list)
    status: RetrievalStatus = RetrievalStatus.EMPTY
    detail: str | None = None
    warning: str | None = None


@dataclass(slots=True)
class RevisionRetrievalResult:
    """理解版本历史的检索结果。

    ``revisions`` 按 conceptId 分组；``status=DEGRADED`` 表示至少一次请求失败，
    此时 ``revisions`` 里可能仍有部分概念的历史。DEGRADED 绝不能解释为
    「用户过去没有误区 / 没有变化」。
    """

    revisions: dict[int, list[UnderstandingRevision]] = field(default_factory=dict)
    status: RetrievalStatus = RetrievalStatus.EMPTY
    warning: str | None = None
    failed_concept_ids: list[int] = field(default_factory=list)


def normalize_understanding_result(value: object) -> UnderstandingRetrievalResult:
    """兼容旧检索源：返回裸列表时按「已查询，结果有无」降级为 EMPTY / SUCCESS。

    旧契约无法区分「查询失败」与「确实为空」；这里不伪造成功，也不把它当故障：
    受控测试源与 Fixture 返回的空列表明确表示「确实没有」。HTTP 检索源必须返回
    ``UnderstandingRetrievalResult``，否则无法表达 DEGRADED。
    """
    if isinstance(value, UnderstandingRetrievalResult):
        return value
    if isinstance(value, list):
        items = list(value)
        return UnderstandingRetrievalResult(
            items=items,
            status=RetrievalStatus.SUCCESS if items else RetrievalStatus.EMPTY,
        )
    return UnderstandingRetrievalResult(
        status=RetrievalStatus.DEGRADED,
        warning="学习认知检索返回格式异常，无法确认是否已有认知记录。",
    )


def normalize_memory_result(value: object) -> MemoryRetrievalResult:
    """兼容旧检索源：裸列表按「已查询，结果有无」降级为 EMPTY / SUCCESS。"""
    if isinstance(value, MemoryRetrievalResult):
        return value
    if isinstance(value, list):
        items = list(value)
        return MemoryRetrievalResult(
            items=items,
            status=RetrievalStatus.SUCCESS if items else RetrievalStatus.EMPTY,
        )
    return MemoryRetrievalResult(
        status=RetrievalStatus.DEGRADED,
        warning="记忆检索返回格式异常，无法确认是否有相关记忆。",
    )


def normalize_revision_result(value: object) -> RevisionRetrievalResult:
    """兼容旧检索源：裸 dict 按「是否取到任意版本」降级为 EMPTY / SUCCESS。"""
    if isinstance(value, RevisionRetrievalResult):
        return value
    if isinstance(value, dict):
        revisions = {
            int(concept_id): list(items or [])
            for concept_id, items in value.items()
        }
        return RevisionRetrievalResult(
            revisions=revisions,
            status=(
                RetrievalStatus.SUCCESS
                if any(revisions.values())
                else RetrievalStatus.EMPTY
            ),
        )
    return RevisionRetrievalResult(
        status=RetrievalStatus.DEGRADED,
        warning="历史轨迹检索返回格式异常，无法确认用户过去是否有变化。",
    )


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


class EvidenceStatus(str, Enum):
    """一次 Evidence 检索的结果状态。

    **不能把 DEGRADED 当成 EMPTY**：接口失败时如果把空列表交给上层，
    上下文就会把「证据服务坏了」说成「这个用户没有历史证据」，
    模型会据此断言用户从未学过相关内容 —— 这是一个不会报错的错误答案。
    """

    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    DEGRADED = "DEGRADED"


@dataclass(slots=True)
class EvidenceRetrievalResult:
    """Evidence 检索结果：原始条目 + 查询状态 + 可观测告警。

    - SUCCESS：查询成功且返回至少一条证据；
    - EMPTY：查询成功，但用户在这些概念上确实没有证据；
    - DEGRADED：查询失败或响应不完整，不能据此判断“没有历史”。

    warnings 是可读字符串，包含 spaceId / conceptIds 与错误摘要，
    会同时进入 trace、meta warnings 和上下文的「本轮证据检索状态」区块。
    """

    items: list[EvidenceItem] = field(default_factory=list)
    status: EvidenceStatus = EvidenceStatus.EMPTY
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_items(
        cls, items: list[EvidenceItem], *, warnings: list[str] | None = None
    ) -> "EvidenceRetrievalResult":
        cleaned = list(items or [])
        return cls(
            items=cleaned,
            status=EvidenceStatus.SUCCESS if cleaned else EvidenceStatus.EMPTY,
            warnings=list(warnings or []),
        )

    @classmethod
    def degraded(
        cls,
        warnings: str | list[str],
        *,
        items: list[EvidenceItem] | None = None,
    ) -> "EvidenceRetrievalResult":
        normalized = [warnings] if isinstance(warnings, str) else list(warnings)
        return cls(
            items=list(items or []),
            status=EvidenceStatus.DEGRADED,
            warnings=normalized,
        )

    @property
    def is_degraded(self) -> bool:
        return self.status is EvidenceStatus.DEGRADED

    @property
    def warning_text(self) -> str | None:
        return "；".join(item for item in self.warnings if item) or None


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
    ) -> UnderstandingRetrievalResult:
        """用户在这些 Concept 上的当前认知快照。

        必须区分「确实没有快照」（EMPTY）与「查询失败/响应不完整」（DEGRADED）。
        """
        ...

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> MemoryRetrievalResult:
        """与当前话题相关的长期 Memory。只取 ACTIVE —— CANDIDATE 还没被用户确认。

        UNAVAILABLE / DEGRADED_* 必须保留为 DEGRADED，不能折叠成「没有记忆」。
        """
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
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        """判定依据。按需召回，默认条数很小 —— 不是每次回答都需要它。

        返回**有界**结果（服务端按 limit 截断），并显式区分三种状态：
        查到了（SUCCESS）、确实没有（EMPTY）、查询失败（DEGRADED）。
        调用方不得把 DEGRADED 降级成空列表，否则会把故障说成“没有历史”。
        """
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
        self,
        user_id: int,
        concept_ids: list[int],
        *,
        space_id: int | None = None,
    ) -> RevisionRetrievalResult:
        """理解快照的不可变版本历史，按 conceptId 分组。

        只有显式复盘才调用；它用于找到「误区何时出现、何时被纠正」以及
        每次变化的 triggerEvidenceId，而不是把所有版本无差别塞进上下文。
        查询失败必须是 DEGRADED，不能伪装成「没有历史版本」。

        ``space_id`` 为可选：HTTP 实现用它触发后端的跨 Space 断言；
        不传时保持旧行为，兼容既有实现与调用方。
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
