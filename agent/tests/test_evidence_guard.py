"""非成功知识检索状态的运行时兜底测试。

这些用例不联网、不调用真实模型：它们钉死的是 Runtime 的确定性行为 ——
当知识检索明确没有可用资料时，最终回答不能一个字都不提，同时普通寒暄
不能被这条规则打扰，成功检索也不能被追加任何说明。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings
from app.evidence_guard import (
    GUARD_STATUSES,
    already_discloses,
    evidence_status_notice,
    is_knowledge_request,
)
from app.llm.base import ChatMessage
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
)
from app.runtime import AgentRuntime
from app.schemas import ChatRequest, Concept, KnowledgeChunk
from app.trace import TraceEventType
from evaluation.rag import DATASET_V2_PATH
from evaluation.rag.models import load_cases

_CASES = {case.id: case for case in load_cases(DATASET_V2_PATH)}


class FakeLLM:
    name = "fake"

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield self.answer

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self.answer

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    name = "fake-retriever"

    def __init__(
        self,
        status: KnowledgeStatus,
        *,
        chunk: KnowledgeChunk | None = None,
        warning: str | None = None,
    ) -> None:
        self.status = status
        self.chunk = chunk
        self.warning = warning

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return []

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(
            chunks=[self.chunk] if self.chunk else [],
            status=self.status,
            warning=self.warning,
        )

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ):
        return EvidenceRetrievalResult()

    async def aclose(self) -> None:
        return None


def _request(question: str) -> ChatRequest:
    return ChatRequest(
        runId=1,
        userId=1,
        conversationId=1,
        learningSpaceId=1,
        userMessage=question,
    )


async def _ask(status: KnowledgeStatus, question: str, answer: str, *, chunk=None):
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        FakeLLM(answer),
    )
    prepared = await runtime.prepare(
        _request(question),
        retriever=FakeRetriever(status, chunk=chunk),
    )
    try:
        parts = []
        async for delta in runtime.generate(prepared):
            parts.append(delta)
        return "".join(parts), prepared
    finally:
        await runtime.aclose()


def _admission_matches(case_id: str, text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _CASES[case_id].admission_patterns)


@pytest.mark.parametrize(
    ("status", "case_id", "question", "answer", "expected"),
    [
        (
            KnowledgeStatus.THRESHOLD_FILTERED,
            "D01",
            "请解释量子色动力学中的渐进自由。",
            "量子色动力学中的渐进自由是指强相互作用在高能下变弱。",
            "本轮检索到的文档片段相关度不足",
        ),
        (
            KnowledgeStatus.NO_MATCH,
            "F02",
            "请解释量子色动力学中的渐进自由。",
            "量子色动力学中的渐进自由是指强相互作用在高能下变弱。",
            "本轮上传资料中没有检索到",
        ),
        (
            KnowledgeStatus.NO_VALID_DOCUMENTS,
            "F01",
            "ZenFrame 的提醒模块通过什么协议发送云端定时提醒？",
            "ZenFrame 通过 MQTT 发送云端定时提醒。",
            "本轮没有可用且已就绪的上传文档",
        ),
        (
            KnowledgeStatus.DEGRADED,
            "F04",
            "ZenFrame 的提醒模块通过什么协议发送云端定时提醒？",
            "ZenFrame 实际采用 MQTT。",
            "本轮知识检索不可用",
        ),
    ],
)
async def test_guard_appends_a_status_notice_when_model_does_not(
    status, case_id, question, answer, expected
) -> None:
    output, prepared = await _ask(status, question, answer)

    assert expected in output
    assert _admission_matches(case_id, output)
    # 说明本身不能带引用；否则等于在无证据时伪造来源。
    assert "[资料《" not in output.split("（本轮")[0]

    event = next(
        item for item in prepared.trace.events if item.type is TraceEventType.EVIDENCE_GUARD
    )
    assert event.detail == {"status": status.value, "added": True, "hasKnowledge": False}


async def test_guard_does_not_touch_ordinary_chat() -> None:
    for question, answer in [
        ("今天天气怎么样？", "今天晴，适合出门。"),
        ("你好", "你好！有什么可以帮你？"),
        ("好的，谢谢", "不客气。"),
    ]:
        output, prepared = await _ask(KnowledgeStatus.NO_MATCH, question, answer)

        assert output == answer
        assert "本轮上传资料" not in output
        event = next(
            item for item in prepared.trace.events if item.type is TraceEventType.EVIDENCE_GUARD
        )
        assert event.detail["added"] is False


async def test_guard_does_not_duplicate_an_existing_disclosure() -> None:
    answer = "本轮上传资料中没有检索到直接依据。以下只是通用知识。"
    output, _ = await _ask(
        KnowledgeStatus.THRESHOLD_FILTERED,
        "请解释量子色动力学中的渐进自由。",
        answer,
    )

    assert output == answer
    assert output.count("本轮上传资料中没有检索到") == 1
    assert already_discloses("THRESHOLD_FILTERED", answer) is True


async def test_budget_dropped_success_is_explained_by_context_not_contradicted_by_guard() -> None:
    """检索成功但知识全被预算丢掉时，Context 说「检索到了」，Guard 不能再说「没有资料」。"""
    chunk = KnowledgeChunk(
        id=9,
        document_id=10,
        chunk_index=0,
        document_name="orbit_current.md",
        content="ORBIT-4721 当前调度器使用 17 秒的重试窗口。" * 20,
    )
    answer = "当前是 17 秒。"
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=100),
        FakeLLM(answer),
    )
    prepared = await runtime.prepare(
        _request("ORBIT-4721 当前重试窗口是多少？"),
        retriever=FakeRetriever(KnowledgeStatus.SUCCESS, chunk=chunk),
    )
    try:
        output = "".join([delta async for delta in runtime.generate(prepared)])
    finally:
        await runtime.aclose()

    assert prepared.context.knowledge_state == "SUCCESS_BUT_DROPPED"
    assert "SUCCESS_BUT_DROPPED" in prepared.context.messages[0].content
    assert "检索成功" in prepared.context.messages[0].content
    # Guard 不追加「没有检索到资料」，否则会与 Context 状态直接矛盾。
    assert output == answer
    assert "本轮上传资料中没有检索到" not in output
    assert not any(
        item.type is TraceEventType.EVIDENCE_GUARD for item in prepared.trace.events
    )
    assert "SUCCESS_BUT_DROPPED" not in GUARD_STATUSES
    assert evidence_status_notice("SUCCESS_BUT_DROPPED", "这是什么？", answer) is None


async def test_successful_retrieval_is_unchanged() -> None:
    chunk = KnowledgeChunk(
        id=1,
        document_id=10,
        chunk_index=0,
        document_name="zenframe.md",
        content="ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。",
    )
    answer = "ZenFrame 使用 MQTT [资料《zenframe.md》#0]。"
    output, prepared = await _ask(
        KnowledgeStatus.SUCCESS,
        "ZenFrame 的提醒模块通过什么协议发送云端定时提醒？",
        answer,
        chunk=chunk,
    )

    assert output == answer
    assert prepared.trace.as_dict()["events"]
    assert not any(
        item.type is TraceEventType.EVIDENCE_GUARD for item in prepared.trace.events
    )


async def test_notice_is_streamed_as_a_normal_delta_and_keeps_sse_contract() -> None:
    runtime = AgentRuntime(
        Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=3000),
        FakeLLM("量子色动力学中的渐进自由是指强相互作用在高能下变弱。"),
    )
    try:
        events = [
            event
            async for event in runtime.stream(
                _request("请解释量子色动力学中的渐进自由。")
            )
        ]
    finally:
        await runtime.aclose()

    assert events[0].event == "meta"
    assert events[-1].event == "done"
    assert all(event.event == "delta" for event in events[1:-1])
    assert not any(event.event == "error" for event in events)
    joined = "".join(str(event.data["delta"]) for event in events if event.event == "delta")
    assert "本轮上传资料中没有检索到" in joined


def test_question_shape_heuristic_is_deliberately_narrow() -> None:
    assert is_knowledge_request("请解释量子色动力学中的渐进自由。") is True
    assert is_knowledge_request("ZenFrame 的提醒模块通过什么协议发送？") is True
    assert is_knowledge_request("TLB 的核心要点是什么？") is True
    assert is_knowledge_request("今天天气怎么样？") is False
    assert is_knowledge_request("为什么天空是蓝的？") is False
    assert is_knowledge_request("你好") is False


def test_history_and_personal_questions_do_not_get_a_knowledge_status_notice() -> None:
    """知识状态兜底只服务知识问句，不能在复盘 / 个人历史问题上加一段资料说明。"""

    history_questions = [
        # C：显式复盘，Trajectory-enabled 与 Current-only 都不该触发。
        "我之前对 TLB 的理解错在哪里？后来是怎么纠正的？",
        # D：对当前理解的个人问题，不是资料检索问题。
        "当前应如何理解 TLB？",
    ]
    for question in history_questions:
        assert is_knowledge_request(question) is False
        assert evidence_status_notice("NO_MATCH", question, "回答") is None

    # 混合问题确实在问知识，仍应触发状态说明。
    mixed = "TLB 的核心要点是什么？我之前哪里理解错了？"
    assert is_knowledge_request(mixed) is True
    assert evidence_status_notice("NO_MATCH", mixed, "回答") is not None

    assert evidence_status_notice(
        "NO_MATCH", "请解释量子色动力学中的渐进自由。", "回答"
    )
    assert evidence_status_notice("NO_MATCH", "今天天气怎么样？", "回答") is None
    assert evidence_status_notice("SUCCESS", "请解释量子色动力学中的渐进自由。", "回答") is None
    assert GUARD_STATUSES == {
        "NO_VALID_DOCUMENTS",
        "NO_MATCH",
        "THRESHOLD_FILTERED",
        "DEGRADED",
    }
