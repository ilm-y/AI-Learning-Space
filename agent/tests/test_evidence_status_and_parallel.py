"""E3：Evidence 三态语义、有界批量 HTTP 契约与 E1 并行化回归。

这些测试不连 MySQL / Qdrant / 真实后端：

- Runtime 路径用可控 Retriever 固定「查询状态 → state / ContextBuilder」的传播；
- 并行性用「四路必须同时到达再放行」的栅栏证明，而不是断言调用顺序；
- HTTP 客户端用 httpx.MockTransport 校验 URL / 参数 / 状态解析。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable

import httpx
import pytest

from app.config import Settings
from app.context.builder import ContextBuilder
from app.llm.base import ChatMessage
from app.retriever.base import (
    EvidenceRetrievalResult,
    EvidenceStatus,
    KnowledgeResult,
    KnowledgeStatus,
    RelatedConceptResult,
    RelatedConceptStatus,
)
from app.retriever.http_backend import HttpBackendRetriever
from app.runtime import AgentRuntime
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


def request(question: str, *, user_id: int = 1, space_id: int = 1) -> ChatRequest:
    return ChatRequest(
        runId=1,
        userId=user_id,
        conversationId=1,
        learningSpaceId=space_id,
        userMessage=question,
    )


def runtime() -> AgentRuntime:
    return AgentRuntime(
        Settings(
            _env_file=None,
            retriever_mode="fixture",
            max_context_tokens=3000,
            evidence_related_concepts_limit=3,
            evidence_related_evidence_limit=2,
        ),
        FakeLLM(),
    )


class StatusRetriever:
    """按 concept_ids 返回预设 Evidence 结果的检索源。"""

    name = "status"

    def __init__(
        self,
        evidence_results: Callable[[list[int], int], EvidenceRetrievalResult],
        *,
        related: RelatedConceptResult | None = None,
    ) -> None:
        self._evidence_results = evidence_results
        self._related = related or RelatedConceptResult(
            status=RelatedConceptStatus.EMPTY
        )
        self.evidence_calls: list[tuple[list[int], int]] = []

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return [concept(1, "导数")]

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
        return self._evidence_results(list(concept_ids), limit)

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        return self._related

    async def aclose(self) -> None:
        return None


class ParallelRetriever:
    """四路检索必须同时到达栅栏，否则超时失败。

    U / M / 原始 Evidence / 相关概念各自在入口处报到，等第 4 路到齐才继续。
    如果 Runtime 仍把它们串行发出，第一路会一直等到超时 —— 测试因此能区分
    「结果相同」与「真的并行」。
    """

    name = "parallel"

    def __init__(self, *, related_error: Exception | None = None) -> None:
        self._related_error = related_error
        self.arrived: list[str] = []
        self._gate = asyncio.Event()

    async def _arrive(self, name: str) -> None:
        self.arrived.append(name)
        if len(self.arrived) >= 4:
            self._gate.set()
        await asyncio.wait_for(self._gate.wait(), timeout=2)

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return [concept(1, "导数")]

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        await self._arrive("understandings")
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        await self._arrive("memories")
        return []

    async def get_knowledge(self, query) -> KnowledgeResult:
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        await self._arrive("evidences")
        return EvidenceRetrievalResult.from_items([evidence(11, 1, "导数证据")])

    async def get_related_concepts(
        self,
        user_id: int,
        space_id: int,
        concept_ids: list[int],
        *,
        relation_types: tuple[str, ...] = ("PREREQUISITE", "RELATED"),
        limit: int = 3,
    ) -> RelatedConceptResult:
        await self._arrive("related")
        if self._related_error is not None:
            raise self._related_error
        return RelatedConceptResult(status=RelatedConceptStatus.EMPTY)

    async def aclose(self) -> None:
        return None


QUESTION = "为什么导数的定义是这样？依据是什么"


async def prepare(retriever, *, question: str = QUESTION):
    return await asyncio.wait_for(
        runtime().prepare(request(question), retriever=retriever), timeout=5
    )


# ---------- E3-A：三态传播到 state 与 ContextBuilder ----------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (EvidenceRetrievalResult.from_items([evidence(11, 1, "导数证据")]), "SUCCESS"),
        (EvidenceRetrievalResult.from_items([]), "EMPTY"),
        (EvidenceRetrievalResult.degraded("证据检索失败：backend 500"), "DEGRADED"),
    ],
)
async def test_evidence_status_is_propagated_to_state_and_context(result, expected) -> None:
    retriever = StatusRetriever(lambda concept_ids, limit: result)
    prepared = await prepare(retriever)
    state = prepared.state

    assert result.status.value == expected
    assert state.evidence_status == expected
    assert prepared.context.evidence_status == expected
    if expected == "DEGRADED":
        assert state.evidence_warning == "证据检索失败：backend 500"
        assert prepared.context.evidence_warning == "证据检索失败：backend 500"
    else:
        assert state.evidence_warning is None
        assert prepared.context.evidence_warning is None


async def test_success_and_empty_do_not_emit_degraded_notice() -> None:
    for result in (
        EvidenceRetrievalResult.from_items([evidence(11, 1, "导数证据")]),
        EvidenceRetrievalResult.from_items([]),
    ):
        prepared = await prepare(StatusRetriever(lambda concept_ids, limit: result))
        instruction = prepared.context.system_instruction

        assert "本轮证据检索状态" not in instruction
        assert "本轮证据检索部分失败" not in instruction
        assert all(section.name != "evidence_status" for section in prepared.context.sections)


async def test_degraded_emits_notice_and_is_not_treated_as_no_history() -> None:
    warning = "证据检索失败（spaceId=1, conceptIds=[1]）：后端返回 500"
    prepared = await prepare(
        StatusRetriever(lambda concept_ids, limit: EvidenceRetrievalResult.degraded(warning))
    )
    context = prepared.context
    instruction = context.system_instruction

    # 状态区块存在，且保留原始 warning。
    status_sections = [section for section in context.sections if section.name == "evidence_status"]
    assert len(status_sections) == 1
    assert "DEGRADED" in status_sections[0].content
    assert warning in status_sections[0].content
    assert warning in context.warnings

    # 明确告诉模型：不能据此判断「没有历史」。
    assert "本轮证据检索部分失败，不能据此判断学习者没有相关历史" in instruction
    assert "不能据此判断学习者没有相关历史" in instruction
    assert "不要声称用户没有证据" in instruction

    # 没有认知记录时，也不能落到普通的「没有任何历史认知记录」措辞。
    assert "本次没有检索到任何历史认知记录" not in instruction


async def test_cross_concept_degraded_is_visible_and_marked() -> None:
    related = RelatedConceptResult(
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
    )

    def results(concept_ids: list[int], limit: int) -> EvidenceRetrievalResult:
        if concept_ids == [1]:
            return EvidenceRetrievalResult.from_items([evidence(11, 1, "导数证据")])
        return EvidenceRetrievalResult.degraded("跨概念证据检索失败：backend timeout")

    prepared = await prepare(StatusRetriever(results, related=related))
    state = prepared.state
    context = prepared.context

    assert state.evidence_status == "SUCCESS"
    assert state.cross_concept_evidence_status == "DEGRADED"
    assert state.cross_concept_evidence_warning == "跨概念证据检索失败：backend timeout"
    assert context.cross_concept_evidence_status == "DEGRADED"
    assert "跨概念证据检索失败：backend timeout" in context.warnings
    assert "跨概念证据检索：DEGRADED" in context.system_instruction
    assert "不能据此判断这些相关概念没有历史证据" in context.system_instruction


# ---------- E3-C：并行化与关系失败隔离 ----------


async def test_original_evidence_and_um_run_in_parallel_with_relations() -> None:
    retriever = ParallelRetriever()
    prepared = await prepare(retriever)
    state = prepared.state

    # 四路都在等同一个栅栏；串行实现会在第一路超时。
    assert set(retriever.arrived) == {
        "understandings",
        "memories",
        "evidences",
        "related",
    }
    assert [item.id for item in state.evidences] == [11]
    assert state.evidence_status == "SUCCESS"


async def test_relation_failure_does_not_block_original_evidence_or_um() -> None:
    retriever = ParallelRetriever(related_error=RuntimeError("relation backend down"))
    prepared = await prepare(retriever)
    state = prepared.state

    assert set(retriever.arrived) == {
        "understandings",
        "memories",
        "evidences",
        "related",
    }
    # 原始 Evidence 照常成功，不被关系失败连带成降级。
    assert [item.id for item in state.evidences] == [11]
    assert state.evidence_status == "SUCCESS"
    assert state.expanded_concepts == []
    assert state.related_concepts_status == RelatedConceptStatus.FAILED.value
    assert state.related_concepts_warning


async def test_original_and_cross_evidence_are_merged_and_deduplicated() -> None:
    related = RelatedConceptResult(
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
    )

    def results(concept_ids: list[int], limit: int) -> EvidenceRetrievalResult:
        if concept_ids == [1]:
            return EvidenceRetrievalResult.from_items([evidence(11, 1, "导数证据")])
        # 跨概念查询里混入了原始概念的证据（11）；它与原始结果重复，必须去重。
        # 21 是概念 2 的真实历史证据，必须保留并打上跨概念标记。
        return EvidenceRetrievalResult.from_items(
            [evidence(11, 1, "导数证据"), evidence(21, 2, "极限历史证据")]
        )

    prepared = await prepare(StatusRetriever(results, related=related))
    state = prepared.state

    assert [item.id for item in state.evidences] == [11, 21]
    assert [item.id for item in state.cross_concept_evidences] == [21]
    assert state.evidences[0].is_cross_concept is False
    cross = state.cross_concept_evidences[0]
    assert cross.is_cross_concept is True
    assert cross.source_concept_id == 1
    assert cross.related_via == "PREREQUISITE:极限"
    assert cross.concept_id == 2
    assert state.cross_concept_evidence_status == "SUCCESS"


async def test_evidence_uses_configured_bounded_limits() -> None:
    retriever = StatusRetriever(
        lambda concept_ids, limit: EvidenceRetrievalResult.from_items(
            [evidence(11, 1, "导数证据")]
        )
    )
    prepared = await prepare(retriever)

    # 原始概念固定 evidence_top_k，不因扩展被挤占。
    assert retriever.evidence_calls == [([1], 3)]
    assert prepared.state.evidence_status == "SUCCESS"


# ---------- E3-B：HTTP 有界批量契约 ----------


def http_retriever(handler) -> HttpBackendRetriever:
    retriever = HttpBackendRetriever("http://backend.test", auth_token="user-jwt")
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever


async def test_http_evidence_uses_one_bounded_batch_request() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "spaceId": request.url.params.get("spaceId"),
                "conceptIds": request.url.params.get("conceptIds"),
                "limit": request.url.params.get("limit"),
            }
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {"id": 11, "conceptId": 1, "observation": "导数证据"},
                    {"id": 21, "conceptId": 2, "observation": "极限证据"},
                ],
            },
        )

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_evidences(7, 9, [1, 2, 1], 5)
    finally:
        await retriever.aclose()

    # 多个 concept 只发一次请求，且不在客户端切片。
    assert len(seen) == 1
    assert seen[0] == {
        "path": "/api/evidences",
        "spaceId": "9",
        "conceptIds": "1,2",
        "limit": "5",
    }
    assert result.status is EvidenceStatus.SUCCESS
    assert [item.id for item in result.items] == [11, 21]


async def test_http_evidence_empty_is_empty_not_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": []})

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_evidences(7, 9, [1], 3)
    finally:
        await retriever.aclose()

    assert result.status is EvidenceStatus.EMPTY
    assert result.items == []
    assert result.warning_text is None


async def test_http_evidence_backend_failure_is_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "message": "boom"})

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_evidences(7, 9, [1], 3)
    finally:
        await retriever.aclose()

    assert result.status is EvidenceStatus.DEGRADED
    assert result.items == []
    assert result.warning_text is not None
    assert "spaceId=9" in result.warning_text
    assert "[1]" in result.warning_text


async def test_http_evidence_null_data_is_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": None})

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_evidences(7, 9, [1], 3)
    finally:
        await retriever.aclose()

    assert result.status is EvidenceStatus.DEGRADED
    assert result.warning_text is not None


async def test_http_evidence_partial_parse_is_degraded_but_keeps_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {"id": 11, "conceptId": 1, "observation": "导数证据"},
                    {"conceptId": 2, "observation": "缺少 id"},
                ],
            },
        )

    retriever = http_retriever(handler)
    try:
        result = await retriever.get_evidences(7, 9, [1, 2], 3)
    finally:
        await retriever.aclose()

    assert result.status is EvidenceStatus.DEGRADED
    assert [item.id for item in result.items] == [11]
    assert result.warning_text is not None
    assert "1 条无法解析" in result.warning_text


async def test_http_evidence_no_request_when_limit_or_concepts_empty() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": []})

    retriever = http_retriever(handler)
    try:
        assert (await retriever.get_evidences(7, 9, [], 3)).status is EvidenceStatus.EMPTY
        assert (await retriever.get_evidences(7, 9, [1], 0)).status is EvidenceStatus.EMPTY
    finally:
        await retriever.aclose()

    assert calls == []


# ---------- ContextBuilder 单元级断言：状态字段进入 preview ----------


def test_context_preview_exposes_evidence_status() -> None:
    from app.state.agent_state import AgentState
    from app.trace import RunTrace

    state = AgentState.from_request(request(QUESTION), RunTrace(run_id=1))
    state.evidence_status = "DEGRADED"
    state.evidence_warning = "证据检索失败：backend 500"
    state.cross_concept_evidence_status = "EMPTY"

    preview = ContextBuilder().build(state).as_dict()

    assert preview["evidenceStatus"] == "DEGRADED"
    assert preview["evidenceWarning"] == "证据检索失败：backend 500"
    assert preview["crossConceptEvidenceStatus"] == "EMPTY"
    assert preview["crossConceptEvidenceWarning"] is None
