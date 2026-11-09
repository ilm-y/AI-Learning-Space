"""D4-5 评测用的 Fake Retriever。

只实现 `app.retriever.base.Retriever` 中评测需要的方法，数据来自声明式 fixture。
它不是生产检索源，也不模拟网络：所有状态（SUCCESS / EMPTY / DEGRADED /
NO_MATCH …）都按 case 预置，评测只验证 Runtime 的执行与 ContextBuilder 的组装。

评测脚本不会调用 `AgentRuntime._retrieve`：真实 Runtime 还会跑规则识别、Resolver
与并发编排，这些属于 E1/E2，不属于 D4-5。这里按 `RetrievalPlan` 的触发结果
调用对应方法，模拟 Runtime 在检索层的接线方式。
"""

from __future__ import annotations

from app.retriever.base import (
    EvidenceRetrievalResult,
    EvidenceStatus,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
    MemoryRetrievalResult,
    RelatedConceptResult,
    RevisionRetrievalResult,
    RetrievalStatus,
    UnderstandingRetrievalResult,
)
from app.schemas import (
    Concept,
    EvidenceItem,
    KnowledgeChunk,
    MemoryItem,
    Understanding,
    UnderstandingRevision,
)
from tests.eval.context_quality.dataset import CATALOG, Case, Retrieved


class FakeRetriever:
    """按 case 预置结果返回；同时记录调用次数，便于核对触发策略。"""

    name = "fake:d4-5"

    def __init__(self, case: Case, retrieved: Retrieved | None = None) -> None:
        self._case = case
        self._data = retrieved or case.retrieved
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def list_concepts(self, space_id: int) -> list[Concept]:
        self._count("list_concepts")
        return list(CATALOG)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> UnderstandingRetrievalResult:
        self._count("understandings")
        data = self._data
        return UnderstandingRetrievalResult(
            items=list(data.understandings),
            status=RetrievalStatus(data.understanding_status),
            warning=data.understanding_warning,
        )

    async def get_memories(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        query: str,
    ) -> MemoryRetrievalResult:
        self._count("memories")
        data = self._data
        return MemoryRetrievalResult(
            items=list(data.memories),
            status=RetrievalStatus(data.memory_status),
            detail=data.memory_detail,
            warning=data.memory_warning,
        )

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        self._count("knowledge")
        data = self._data
        return KnowledgeResult(
            chunks=list(data.knowledge),
            status=KnowledgeStatus(data.knowledge_status),
            warning=data.knowledge_warning,
        )

    async def get_evidences(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        limit: int,
    ) -> EvidenceRetrievalResult:
        self._count("evidences")
        data = self._data
        return EvidenceRetrievalResult(
            items=list(data.evidences),
            status=EvidenceStatus(data.evidence_status),
            warnings=[data.evidence_warning] if data.evidence_warning else [],
        )

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        self._count("related")
        return RelatedConceptResult()

    async def get_revisions(
        self, user_id: int, concept_ids: list[int]
    ) -> RevisionRetrievalResult:
        self._count("revisions")
        data = self._data
        return RevisionRetrievalResult(
            revisions={
                concept_id: list(items)
                for concept_id, items in data.revisions.items()
            },
            status=RetrievalStatus(data.revision_status),
            warning=data.revision_warning,
        )

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ) -> list[EvidenceItem]:
        self._count("evidence_details")
        wanted = set(evidence_ids)
        return [item for item in self._data.evidence_details if item.id in wanted]

    async def aclose(self) -> None:
        self._count("aclose")
