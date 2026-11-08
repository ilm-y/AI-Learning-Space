"""E0/E1：Evidence 受控一跳跨概念扩展的离线回归测试。

这些测试不连 MySQL / Qdrant / 真实后端：
- Runtime 路径用 FakeRetriever 控制概念、关系与 Evidence；
- HTTP 客户端用 httpx.MockTransport 校验 URL / 参数 / 解析与失败语义。
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.context.builder import ContextBuilder
from app.llm.base import ChatMessage
from app.retriever.base import (
    KnowledgeResult,
    KnowledgeStatus,
    RelatedConceptResult,
    RelatedConceptStatus,
)
from app.retriever.http_backend import HttpBackendRetriever
from app.runtime import AgentRuntime, _select_expansion_concepts
from app.schemas import (
    ChatRequest,
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    RelatedConcept,
)


class FakeLLM:
    name = "fake"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "回答"

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return "回答"

    async def aclose(self) -> None:
        return None


def concept(concept_id: int, name: str) -> Concept:
    return Concept(id=concept_id, canonical_name=name)


def evidence(evidence_id: int, concept_id: int, text: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        concept_id=concept_id,
        observation=text,
        correctness=EvidenceCorrectness.CORRECT,
    )


class FakeRetriever:
    """可控检索源：关系与 Evidence 都按 concept 预置。"""

    name = "fake"

    def __init__(
        self,
        concepts: list[Concept],
        *,
        related: RelatedConceptResult | None = None,
        related_error: Exception | None = None,
        evidences: dict[int, list[EvidenceItem]] | None = None,
    ) -> None:
        self.concepts = concepts
        self.related = related or RelatedConceptResult(status=RelatedConceptStatus.EMPTY)
        self.related_error = related_error
        self.evidences = evidences or {}
        self.related_calls: list[dict[str, object]] = []
        self.evidence_calls: list[tuple[list[int], int]] = []

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return self.concepts

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_knowledge(self, query) -> KnowledgeResult:
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        self.evidence_calls.append((list(concept_ids), limit))
        found: list[EvidenceItem] = []
        for concept_id in concept_ids:
            found.extend(self.evidences.get(concept_id, []))
        return found[:limit]

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        self.related_calls.append(
            {
                "user_id": user_id,
                "space_id": space_id,
                "concept_ids": list(concept_ids),
                "relation_types": relation_types,
                "limit": limit,
            }
        )
        if self.related_error is not None:
            raise self.related_error
        return self.related

    async def aclose(self) -> None:
        return None


def request(question: str, *, user_id: int = 1, space_id: int = 1) -> ChatRequest:
    return ChatRequest(
        runId=1,
        userId=user_id,
        conversationId=1,
        learningSpaceId=space_id,
        userMessage=question,
    )


def runtime(*, related_limit: int = 3, related_evidence_limit: int = 2) -> AgentRuntime:
    return AgentRuntime(
        Settings(
            _env_file=None,
            retriever_mode="fixture",
            max_context_tokens=3000,
            evidence_related_concepts_limit=related_limit,
            evidence_related_evidence_limit=related_evidence_limit,
        ),
        FakeLLM(),
    )


async def prepare(rt: AgentRuntime, retriever: FakeRetriever, question: str, **kwargs):
    return await rt.prepare(request(question, **kwargs), retriever=retriever)


# ---------- 场景 1：只剩「导数」时扩展到前置「极限」 ----------


async def test_prerequisite_expands_evidence_one_hop() -> None:
    derivative = concept(1, "导数")
    limit_concept = concept(2, "极限")
    retriever = FakeRetriever(
        [derivative, limit_concept],
        related=RelatedConceptResult(
            items=[
                RelatedConcept(
                    concept_id=2,
                    concept_name="极限",
                    relation_type="PREREQUISITE",
                    direction="INCOMING",
                    source_concept_id=1,
                )
            ],
            status=RelatedConceptStatus.OK,
        ),
        evidences={
            1: [evidence(11, 1, "你解释过导数的定义。")],
            2: [evidence(21, 2, "你曾用极限描述趋近过程。")],
        },
    )
    rt = runtime()
    prepared = await prepare(rt, retriever, "为什么导数的定义是这样？依据是什么")
    state = prepared.state

    assert [item.concept_id for item in state.expanded_concepts] == [2]
    # 原始概念仍按 evidence_top_k 单独查询，不被扩展挤占。
    assert retriever.evidence_calls[0] == ([1], 3)
    assert retriever.evidence_calls[1] == ([2], 2)
    cross = state.cross_concept_evidences
    assert [item.id for item in cross] == [21]
    marked = cross[0]
    assert marked.is_cross_concept is True
    assert marked.source_concept_id == 1
    assert marked.related_via == "PREREQUISITE:极限"
    # 真实归属不能被改写。
    assert marked.concept_id == 2
    assert [item.id for item in state.evidences] == [11, 21]


# ---------- 场景 2：多概念时原始检索行为不变 ----------


async def test_multiple_detected_concepts_keep_original_retrieval() -> None:
    retriever = FakeRetriever(
        [concept(1, "导数"), concept(2, "极限")],
        evidences={
            1: [evidence(11, 1, "导数证据")],
            2: [evidence(21, 2, "极限证据")],
        },
    )
    rt = runtime()
    prepared = await prepare(rt, retriever, "为什么导数要用极限来定义？")
    state = prepared.state

    assert [item.concept.id for item in state.detected_concepts] == [1, 2]
    # 原始概念仍然一次查询、保持原顺序。
    assert retriever.evidence_calls == [([1, 2], 3)]
    assert [item.id for item in state.evidences] == [11, 21]
    assert all(not item.is_cross_concept for item in state.evidences)
    assert state.expanded_concepts == []


# ---------- 场景 3：没有关系数据时不扩展 ----------


async def test_no_relation_data_does_not_expand() -> None:
    retriever = FakeRetriever(
        [concept(1, "导数")],
        evidences={1: [evidence(11, 1, "导数证据")]},
    )
    rt = runtime()
    prepared = await prepare(rt, retriever, "为什么导数的定义是这样？")
    state = prepared.state

    assert state.related_concepts_status == RelatedConceptStatus.EMPTY.value
    assert state.expanded_concepts == []
    assert len(retriever.evidence_calls) == 1
    assert state.cross_concept_evidences == []


# ---------- 场景 4：查询按请求的 user / space 作用域发出 ----------


async def test_related_query_is_scoped_to_request_user_and_space() -> None:
    retriever = FakeRetriever([concept(1, "导数")])
    rt = runtime()
    prepared = await prepare(
        rt, retriever, "为什么导数是这样？", user_id=7, space_id=9
    )
    state = prepared.state

    assert retriever.related_calls
    call = retriever.related_calls[0]
    assert call["user_id"] == 7
    assert call["space_id"] == 9
    assert call["concept_ids"] == [1]
    # Runtime 只转发当前请求的作用域；跨用户 / 跨 Space 的排除由后端接口负责，
    # 见 ConceptRelationServiceTest 的 crossUser / crossSpace 用例。
    assert state.expanded_concepts == []


# ---------- 场景 5：扩展数量有上限且去重、原始概念优先 ----------


async def test_expansion_is_capped_deduplicated_and_original_first() -> None:
    retriever = FakeRetriever(
        [concept(1, "导数"), concept(2, "极限")],
        related=RelatedConceptResult(
            items=[
                RelatedConcept(
                    concept_id=1,  # 原始概念，必须排除
                    relation_type="RELATED",
                    direction="INCOMING",
                ),
                RelatedConcept(
                    concept_id=2,  # 原始概念，必须排除
                    relation_type="PREREQUISITE",
                    direction="INCOMING",
                ),
                RelatedConcept(
                    concept_id=3,
                    concept_name="连续",
                    relation_type="PREREQUISITE",
                    direction="INCOMING",
                    source_concept_id=1,
                ),
                RelatedConcept(
                    concept_id=4,
                    concept_name="微分",
                    relation_type="RELATED",
                    direction="OUTGOING",
                    source_concept_id=1,
                ),
                RelatedConcept(
                    concept_id=3,  # 重复，必须去重
                    concept_name="连续",
                    relation_type="PREREQUISITE",
                    direction="INCOMING",
                    source_concept_id=2,
                ),
                RelatedConcept(
                    concept_id=5,
                    concept_name="积分",
                    relation_type="RELATED",
                    direction="INCOMING",
                    source_concept_id=1,
                ),
            ],
            status=RelatedConceptStatus.OK,
        ),
    )
    rt = runtime(related_limit=2)
    prepared = await prepare(rt, retriever, "为什么导数和极限是这样？")
    state = prepared.state

    assert [item.concept_id for item in state.expanded_concepts] == [3, 4]


# ---------- 场景 6：Evidence 未触发时不做任何关系查询 ----------


async def test_no_related_query_when_evidence_not_triggered() -> None:
    retriever = FakeRetriever([concept(1, "导数")])
    rt = runtime()
    prepared = await prepare(rt, retriever, "导数的定义是什么")
    state = prepared.state

    assert state.concept_ids == [1]
    assert retriever.related_calls == []
    assert retriever.evidence_calls == []
    assert state.related_concepts_status == "NOT_RETRIEVED"


# ---------- 场景 7：关系查询失败时原始 Evidence 继续，且有可观测 warning ----------


async def test_related_failure_keeps_original_evidence_and_warns() -> None:
    retriever = FakeRetriever(
        [concept(1, "导数")],
        related_error=RuntimeError("relation backend down"),
        evidences={1: [evidence(11, 1, "导数证据")]},
    )
    rt = runtime()
    prepared = await prepare(rt, retriever, "为什么导数是这样？")
    state = prepared.state

    assert state.related_concepts_status == RelatedConceptStatus.FAILED.value
    assert state.related_concepts_warning
    assert state.expanded_concepts == []
    # 原始概念 Evidence 仍按原路径成功返回。
    assert retriever.evidence_calls == [([1], 3)]
    assert [item.id for item in state.evidences] == [11]
    assert any(
        "相关概念查询失败" in warning for warning in prepared.context.warnings
    )
    # FAILED 不能显示为 EMPTY。
    assert state.related_concepts_status != RelatedConceptStatus.EMPTY.value


# ---------- 场景 8：PREREQUISITE 方向假设被测试钉住 ----------


def test_prerequisite_direction_is_source_to_target() -> None:
    """假设：PREREQUISITE(source=A, target=B) 表示 A 是 B 的前置。

    因此查询 B 时，只有 INCOMING（另一端是 source）才算 B 的前置；
    OUTGOING 表示 B 是别人的前置，不能拿来当 B 的前置做扩展。
    """
    incoming = RelatedConcept(
        concept_id=2, relation_type="PREREQUISITE", direction="INCOMING"
    )
    outgoing = RelatedConcept(
        concept_id=3, relation_type="PREREQUISITE", direction="OUTGOING"
    )

    selected = _select_expansion_concepts([1], [incoming, outgoing], limit=3)

    assert [item.concept_id for item in selected] == [2]


# ---------- 上下文注入：跨概念证据单独成块且带标记 ----------


async def test_cross_concept_evidence_is_rendered_separately_with_markers() -> None:
    retriever = FakeRetriever(
        [concept(1, "导数"), concept(2, "极限")],
        related=RelatedConceptResult(
            items=[
                RelatedConcept(
                    concept_id=2,
                    concept_name="极限",
                    relation_type="PREREQUISITE",
                    direction="INCOMING",
                    source_concept_id=1,
                )
            ],
            status=RelatedConceptStatus.OK,
        ),
        evidences={
            1: [evidence(11, 1, "导数证据")],
            2: [evidence(21, 2, "极限历史证据")],
        },
    )
    rt = runtime()
    prepared = await prepare(rt, retriever, "为什么导数的定义是这样？")
    context = prepared.context

    related_sections = [
        section for section in context.sections if section.name.startswith("related_evidence:")
    ]
    assert [section.name for section in related_sections] == ["related_evidence:21"]
    content = related_sections[0].content
    assert "关联依据：PREREQUISITE:极限" in content
    assert "证据归属概念：#2" in content
    assert "经概念 #1 关联" in content
    assert "## 相关概念判定依据（历史参考）" in context.system_instruction
    assert "不能改写" in context.system_instruction
    # 原始证据仍走原区块。
    assert any(section.name == "evidence:11" for section in context.sections)


# ---------- HTTP 客户端：URL / 参数 / 解析 / 失败语义 ----------


def http_retriever(handler) -> HttpBackendRetriever:
    retriever = HttpBackendRetriever("http://backend.test", auth_token="user-jwt")
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever


async def test_http_related_concepts_sends_filters_and_parses() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["types"] = request.url.params.get("types")
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "conceptId": 2,
                        "conceptName": "极限",
                        "relationType": "PREREQUISITE",
                        "weight": 0.9,
                        "direction": "INCOMING",
                    }
                ],
            },
        )

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_related_concepts(
            7, 9, [1], relation_types=("PREREQUISITE", "RELATED"), limit=2
        )
    finally:
        await retriever.aclose()

    assert seen["path"] == "/api/spaces/9/concepts/1/relations"
    assert seen["types"] == "PREREQUISITE,RELATED"
    assert seen["limit"] == "2"
    assert result.status is RelatedConceptStatus.OK
    assert result.items[0].concept_id == 2
    assert result.items[0].source_concept_id == 1
    assert result.items[0].direction == "INCOMING"


async def test_http_related_concepts_partial_failure_is_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1/relations"):
            return httpx.Response(200, json={"code": 0, "data": []})
        return httpx.Response(500, json={"code": 500, "message": "boom"})

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_related_concepts(7, 9, [1, 2], limit=2)
    finally:
        await retriever.aclose()

    assert result.status is RelatedConceptStatus.PARTIAL
    assert result.warning is not None
    assert "2" in result.warning


async def test_http_related_concepts_total_failure_returns_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "message": "boom"})

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_related_concepts(7, 9, [1], limit=2)
    finally:
        await retriever.aclose()

    assert result.status is RelatedConceptStatus.FAILED
    assert result.items == []
    assert result.warning is not None


def test_context_builder_adds_related_warning() -> None:
    """ContextBuilder 只组织已检索数据，不决定去哪检索；但失败原因必须可见。"""
    from app.state.agent_state import AgentState
    from app.trace import RunTrace

    state = AgentState.from_request(request("为什么导数是这样？"), RunTrace(run_id=1))
    state.related_concepts_status = RelatedConceptStatus.FAILED.value
    state.related_concepts_warning = "相关概念查询失败，本次回答未做跨概念证据扩展。"

    context = ContextBuilder().build(state)

    assert any("相关概念查询失败" in warning for warning in context.warnings)


async def test_zero_limit_disables_expansion() -> None:
    retriever = FakeRetriever([concept(1, "导数")])
    rt = runtime(related_limit=0)
    prepared = await prepare(rt, retriever, "为什么导数是这样？")
    state = prepared.state

    assert retriever.related_calls == []
    assert state.related_concepts_status == "NOT_RETRIEVED"


def test_negative_limit_is_rejected_at_config_boundary() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, evidence_related_concepts_limit=-1)
