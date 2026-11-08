"""D4-2A：检索状态契约与失败可见性。

这些测试钉死一条底线：**「没查」「查了但没有」「查失败」是三件事**。
任何把查询失败压成空列表的路径，都会让上下文对模型说「用户没学过 / 没有记忆 /
过去没有误区」—— 这是一次不会报错的错误答案。这里同时覆盖检索层（httpx mock）
和上下文渲染层（ContextBuilder），不连接任何真实外部服务。
"""

from __future__ import annotations

import httpx
import pytest

from app.context.builder import ContextBuilder
from app.llm import build_llm_adapter
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeResult,
    KnowledgeStatus,
    MemoryRetrievalResult,
    RetrievalStatus,
    RevisionRetrievalResult,
    UnderstandingRetrievalResult,
)
from app.retriever.http_backend import HttpBackendRetriever
from app.runtime import AgentRuntime
from app.schemas import (
    Concept,
    Understanding,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace
from tests.helpers import make_request


def _state(message: str = "TLB Miss 是什么？", **overrides) -> AgentState:
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message=message,
        trace=RunTrace(run_id=1),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _retriever(handler) -> HttpBackendRetriever:
    retriever = HttpBackendRetriever(
        "http://backend.test", auth_token="user-jwt", memory_top_k=5
    )
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever


# ===================== 检索层：失败 ≠ 空 =====================


async def test_understanding_query_failure_is_degraded_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/understandings/1":
            return httpx.Response(500, json={"code": 500, "message": "db down"})
        return httpx.Response(200, json={"code": 0, "data": None})

    retriever = _retriever(handler)
    try:
        result = await retriever.get_understandings(7, 9, [1, 2])
    finally:
        await retriever.aclose()

    assert isinstance(result, UnderstandingRetrievalResult)
    assert result.status is RetrievalStatus.DEGRADED
    assert result.items == []
    assert result.failed_concept_ids == [1]
    assert "无法确认" in (result.warning or "")
    # 后端错误正文不进入 warning / prompt。
    assert "db down" not in (result.warning or "")


async def test_understanding_connection_error_is_degraded_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    retriever = _retriever(handler)
    try:
        result = await retriever.get_understandings(7, 9, [1])
    finally:
        await retriever.aclose()

    assert result.status is RetrievalStatus.DEGRADED
    assert result.items == []
    assert result.failed_concept_ids == [1]
    assert "connection refused" not in (result.warning or "")


async def test_understanding_null_snapshot_is_normal_empty() -> None:
    retriever = _retriever(
        lambda request: httpx.Response(200, json={"code": 0, "data": None})
    )
    try:
        result = await retriever.get_understandings(7, 9, [1, 2])
    finally:
        await retriever.aclose()

    assert result.status is RetrievalStatus.EMPTY
    assert result.items == []
    assert result.warning is None


async def test_revision_query_failure_is_degraded_not_no_history() -> None:
    retriever = _retriever(
        lambda request: httpx.Response(500, json={"code": 500, "message": "boom"})
    )
    try:
        result = await retriever.get_revisions(7, [3])
    finally:
        await retriever.aclose()

    assert isinstance(result, RevisionRetrievalResult)
    assert result.status is RetrievalStatus.DEGRADED
    assert result.revisions == {3: []}
    assert result.failed_concept_ids == [3]
    assert "无法确认" in (result.warning or "")
    assert "过去" in (result.warning or "")
    assert "boom" not in (result.warning or "")


# ===================== 上下文层：状态不被压平 =====================


def test_degraded_understanding_and_memory_do_not_read_as_no_records() -> None:
    state = _state(
        understanding_status="DEGRADED",
        understanding_warning="学习认知检索失败或不完整（conceptIds=[10]），无法确认这些知识点是否已有认知记录。",
        memory_status="DEGRADED",
        memory_warning="记忆检索不可用，无法确认是否有相关记忆。",
    )

    context = ContextBuilder().build(state)
    system = context.messages[0].content

    assert "学习认知检索：DEGRADED" in system
    assert "长期记忆检索：DEGRADED" in system
    assert "不能据此判断" in system
    assert "没有检索到任何历史认知记录" not in system
    assert "本次没有检索到" not in system
    assert context.understanding_status == "DEGRADED"
    assert context.memory_status == "DEGRADED"


def test_not_queried_renders_no_negative_implication() -> None:
    context = ContextBuilder().build(_state())

    system = context.messages[0].content
    assert "学习者背景" not in system
    assert "没有检索到任何历史认知记录" not in system
    assert all("没有检索到任何 Understanding" not in warning for warning in context.warnings)
    assert context.understanding_status == "NOT_QUERIED"
    assert context.memory_status == "NOT_QUERIED"
    assert context.revision_status == "NOT_QUERIED"


def test_not_queried_revision_does_not_claim_user_had_no_misconceptions() -> None:
    """普通问题不查 Revision；这不等于「用户过去没有误区」。"""
    understanding = Understanding(
        id=502,
        concept_id=10,
        concept_name="TLB",
        summary="用户理解 TLB 是页表项的缓存。",
        status=UnderstandingStatus.PARTIAL,
    )
    context = ContextBuilder().build(
        _state(
            understandings=[understanding],
            understanding_status="SUCCESS",
            memory_status="EMPTY",
        )
    )

    system = context.messages[0].content
    assert "历史轨迹检索：DEGRADED" not in system
    assert "过去没有误区" not in system
    assert "本轮没有提供历史轨迹" in system
    assert context.revision_status == "NOT_QUERIED"


def test_exposed_understanding_without_body_is_preserved() -> None:
    exposed = Understanding(
        id=501,
        concept_id=10,
        concept_name="Page Table",
        status=UnderstandingStatus.EXPOSED,
    )
    context = ContextBuilder().build(
        _state(
            understandings=[exposed],
            understanding_status="SUCCESS",
            memory_status="EMPTY",
        )
    )

    system = context.messages[0].content
    assert "Page Table" in system
    assert "状态：接触过" in system
    assert "已有状态记录，但当前没有可用的详细内容" in system
    assert any(section.name == "understanding:10" for section in context.sections)


def test_revision_degraded_is_explained_and_forbids_no_history_claim() -> None:
    context = ContextBuilder().build(
        _state(
            understanding_status="SUCCESS",
            memory_status="EMPTY",
            revision_status="DEGRADED",
            revision_warning="历史轨迹检索失败或不完整（conceptIds=[10]），无法确认用户过去是否存在误区或变化。",
        )
    )

    system = context.messages[0].content
    assert "历史轨迹检索：DEGRADED" in system
    assert "不能据此判断用户过去没有误区" in system
    assert context.revision_status == "DEGRADED"


# ===================== Runtime 接线：结果对象 → AgentState → Context =====================


class _DegradedRetriever:
    name = "degraded-fake"

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return [Concept(id=10, canonical_name="TLB", display_name="TLB")]

    async def get_understandings(self, user_id, space_id, concept_ids):
        return UnderstandingRetrievalResult(
            status=RetrievalStatus.DEGRADED,
            warning="学习认知检索失败或不完整（conceptIds=[10]），无法确认这些知识点是否已有认知记录。",
            failed_concept_ids=[10],
        )

    async def get_memories(self, user_id, space_id, concept_ids, query):
        return MemoryRetrievalResult(
            status=RetrievalStatus.DEGRADED,
            detail="UNAVAILABLE",
            warning="记忆检索不可用，无法确认是否有相关记忆。",
        )

    async def get_knowledge(self, query):
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(self, *args):
        return EvidenceRetrievalResult()

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_runtime_propagates_degraded_status_without_false_negation(settings):
    runtime = AgentRuntime(settings, build_llm_adapter(settings))

    prepared = await runtime.prepare(
        make_request("TLB 是什么？"), retriever=_DegradedRetriever()
    )

    assert prepared.state.understanding_status == "DEGRADED"
    assert prepared.state.memory_status == "DEGRADED"
    assert prepared.state.memory_detail == "UNAVAILABLE"
    assert prepared.state.revision_status == "NOT_QUERIED"
    system = prepared.context.messages[0].content
    assert "学习认知检索：DEGRADED" in system
    assert "长期记忆检索：DEGRADED" in system
    assert "没有检索到任何历史认知记录" not in system
