"""离线评测用的确定性世界：概念、关系、Evidence 与检索源。

它**不是**真实模型或真实后端：
- 概念 / 关系是按评测用例手工编排的骨架数据；
- Resolver 的 JSON 输出由 `runner.py` 里的脚本化 LLM 提供；
- 因此这里测的是 Agent 管线的接线、校验与降级语义，不是模型能力。
"""

from __future__ import annotations

from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeResult,
    KnowledgeStatus,
    RelatedConceptResult,
    RelatedConceptStatus,
)
from app.schemas import Concept, EvidenceCorrectness, EvidenceItem, RelatedConcept

# Space 1：微积分
CALCULUS = [
    Concept(id=101, canonical_name="极限", display_name="极限"),
    Concept(id=102, canonical_name="导数", display_name="导数"),
    Concept(id=103, canonical_name="连续性", display_name="连续性"),
    Concept(id=104, canonical_name="微分", display_name="微分"),
]
# Space 2：操作系统
OS = [
    Concept(id=201, canonical_name="页表", display_name="页表"),
    Concept(id=202, canonical_name="TLB", display_name="TLB（快表）"),
]
# Space 3：网络
NETWORK = [
    Concept(id=301, canonical_name="TCP", display_name="TCP"),
    Concept(id=302, canonical_name="拥塞控制", display_name="拥塞控制"),
]
# Space 9：同名概念，用于验证跨 Space 隔离（「极限」id=901）
ALGEBRA = [
    Concept(id=901, canonical_name="极限", display_name="极限（代数）"),
]

SPACES: dict[int, list[Concept]] = {
    1: CALCULUS,
    2: OS,
    3: NETWORK,
    9: ALGEBRA,
}

# 后端关系接口的等价响应：direction 相对被查询概念。
RELATIONS: dict[int, list[RelatedConcept]] = {
    # 102 导数 的前置是 101 极限。
    102: [
        RelatedConcept(
            concept_id=101,
            concept_name="极限",
            relation_type="PREREQUISITE",
            direction="INCOMING",
            source_concept_id=102,
            weight=0.9,
        )
    ],
    # 103 连续性与 101 极限相关（RELATED 双向都算）。
    103: [
        RelatedConcept(
            concept_id=101,
            concept_name="极限",
            relation_type="RELATED",
            direction="INCOMING",
            source_concept_id=103,
            weight=0.7,
        )
    ],
    # 302 拥塞控制 的前置是 301 TCP。
    302: [
        RelatedConcept(
            concept_id=301,
            concept_name="TCP",
            relation_type="PREREQUISITE",
            direction="INCOMING",
            source_concept_id=302,
            weight=0.8,
        )
    ],
    # 201 页表与 202 TLB 相关。
    201: [
        RelatedConcept(
            concept_id=202,
            concept_name="TLB",
            relation_type="RELATED",
            direction="OUTGOING",
            source_concept_id=201,
            weight=0.8,
        )
    ],
}

EVIDENCES: dict[int, list[EvidenceItem]] = {
    101: [
        EvidenceItem(
            id=1011,
            concept_id=101,
            observation="你曾用「无限趋近」解释过极限。",
            correctness=EvidenceCorrectness.CORRECT,
        )
    ],
    102: [
        EvidenceItem(
            id=1021,
            concept_id=102,
            observation="你解释过导数需要先有极限。",
            correctness=EvidenceCorrectness.CORRECT,
        )
    ],
    103: [
        EvidenceItem(
            id=1031,
            concept_id=103,
            observation="你之前把连续性和极限混在一起。",
            correctness=EvidenceCorrectness.PARTIAL,
        )
    ],
    201: [
        EvidenceItem(
            id=2011,
            concept_id=201,
            observation="你画过页表的层级结构。",
            correctness=EvidenceCorrectness.CORRECT,
        )
    ],
    202: [
        EvidenceItem(
            id=2021,
            concept_id=202,
            observation="你提过 TLB 是页表项的缓存。",
            correctness=EvidenceCorrectness.CORRECT,
        )
    ],
    301: [
        EvidenceItem(
            id=3011,
            concept_id=301,
            observation="你描述过 TCP 的可靠传输。",
            correctness=EvidenceCorrectness.CORRECT,
        )
    ],
    302: [
        EvidenceItem(
            id=3021,
            concept_id=302,
            observation="你区分过流量控制和拥塞控制。",
            correctness=EvidenceCorrectness.CORRECT,
        )
    ],
}


class FakeEvalRetriever:
    """按 space_id 返回骨架数据；关系与 Evidence 都按 concept_id 预置。"""

    name = "eval-fake"

    def __init__(self, space_id: int) -> None:
        self.space_id = space_id
        self.related_calls: list[list[int]] = []
        self.evidence_calls: list[tuple[list[int], int]] = []

    async def list_concepts(self, space_id: int) -> list[Concept]:
        if space_id != self.space_id:
            # 后端只会返回当前 Space 的概念；这里同样严格。
            return []
        return list(SPACES.get(space_id, []))

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_knowledge(self, query) -> KnowledgeResult:
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        self.evidence_calls.append((list(concept_ids), limit))
        found: list[EvidenceItem] = []
        for concept_id in concept_ids:
            found.extend(EVIDENCES.get(concept_id, []))
        return EvidenceRetrievalResult.from_items(found[:limit])

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        self.related_calls.append(list(concept_ids))
        items: list[RelatedConcept] = []
        for concept_id in concept_ids:
            items.extend(RELATIONS.get(concept_id, []))
        return RelatedConceptResult(
            items=items[:limit],
            status=RelatedConceptStatus.OK if items else RelatedConceptStatus.EMPTY,
        )

    async def get_revisions(self, user_id: int, concept_ids: list[int]):
        return {}

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ):
        return []

    async def aclose(self) -> None:
        return None
