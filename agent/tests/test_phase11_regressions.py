"""Phase 1.1 缺陷回归：预算口径、History Guard、analyze fixture 保护、auto 可观测性。

这些用例对应 Phase 1 的 FAIL 用例 A06 / A07 / B03 / B06 / D07 / E06。
先写失败断言，再改实现；测试本身就是「修复前后差异」的可复现证据。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.context.builder import ContextBuilder
from app.context.tokens import estimate_tokens
from app.history_guard import build_history_fallback, check_history_answer
from app.llm.mock import MockLLMAdapter
from app.retriever.base import RetrievalError
from app.retriever.routing import build_retriever
from app.runtime import AgentRuntime
from app.schemas import (
    AnalyzeRequest,
    ChatRequest,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceSourceItem,
    EvidenceType,
    HistoryMessage,
    MessageRole,
    Understanding,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "demo_space.json"


def _understanding() -> Understanding:
    return Understanding(
        id=1201,
        concept_id=9201,
        concept_name="TLB",
        summary="用户认为 TLB 缓存的是整个页表。",
        known_parts=[],
        misconceptions=[],
        status=UnderstandingStatus.MISCONCEPTION,
    )


def _evidence() -> EvidenceItem:
    return EvidenceItem(
        id=1704,
        concept_id=9201,
        evidence_type=EvidenceType.TEST_RESULT,
        observation="测试回答：TLB 缓存的是页表项。",
        correctness=EvidenceCorrectness.CORRECT,
        created_at=datetime(2026, 10, 3, 10, 0),
        sources=[EvidenceSourceItem(source_type="TEST_ANSWER", source_id=1704)],
    )


def _state(
    *,
    message: str = "请继续讲 TLB。",
    history: list[HistoryMessage] | None = None,
    evidences: list[EvidenceItem] | None = None,
) -> AgentState:
    return AgentState(
        run_id=1,
        user_id=9001,
        conversation_id=9401,
        space_id=9101,
        user_message=message,
        history=history or [],
        understandings=[_understanding()],
        evidences=evidences or [],
        history_review_requested=True,
        trace=RunTrace(run_id=1),
    )


# ---------- A06：预算丢弃不等于「没有检索到」 ----------


def test_a06_dropped_cognitive_sections_do_not_claim_no_history() -> None:
    context = ContextBuilder(max_context_tokens=60, recent_history_limit=12).build(_state())

    assert context.dropped_sections, "预算 60 应至少丢弃一个区块"
    assert "本次没有检索到任何历史认知记录" not in context.system_instruction
    assert "预算" in context.system_instruction or "未包含" in context.system_instruction


# ---------- A07：预算口径是完整 prompt，超限必须如实报告 ----------


def test_a07_budget_limits_dynamic_context_and_reports_full_prompt_separately() -> None:
    context = ContextBuilder(max_context_tokens=300, recent_history_limit=12).build(_state())

    actual = sum(estimate_tokens(message.content) for message in context.messages)
    assert context.estimated_tokens == actual
    assert context.budget_tokens == 300
    # 契约：300 约束动态上下文（标题/说明/section/消息），固定教学契约不计入；
    # 完整 prompt 仍如实报告，不能把 2500+ 说成 300。
    assert context.context_tokens <= 300
    assert context.budget_exceeded is False
    assert context.estimated_tokens > 300
    assert any(
        "固定系统提示" in warning and "完整 prompt" in warning
        for warning in context.warnings
    )


def test_a07_default_budget_keeps_full_prompt_within_budget() -> None:
    context = ContextBuilder(max_context_tokens=3000, recent_history_limit=12).build(_state())

    assert context.estimated_tokens <= 3000
    assert context.budget_exceeded is False


# ---------- B03：只有按需 evidence、没有轨迹时，也要校验记录事实 ----------


def test_b03_evidence_only_history_is_treated_as_records() -> None:
    state = _state(message="我之前 TLB 错在哪？帮我复盘。", evidences=[_evidence()])

    issues = check_history_answer("记录显示你在 2026-10-03 的测试中答对了三道题。", state)

    assert issues, "只有 1 条证据，不能放行「答对了三道题」"
    assert any(issue.kind in {"record_completeness", "missing_history_acknowledgement"} for issue in issues)


def test_b03_evidence_only_fallback_does_not_deny_evidence() -> None:
    state = _state(message="我之前 TLB 错在哪？帮我复盘。", evidences=[_evidence()])

    fallback = build_history_fallback(state)

    assert "没有关于" not in fallback
    assert "测试回答" in fallback or "TLB 缓存的是页表项" in fallback


def test_b03_explanation_evidence_does_not_support_question_count() -> None:
    """「答对三道题」必须由测试类证据支撑；用户解释不算一次测试。"""
    explanation = EvidenceItem(
        id=1705,
        concept_id=9201,
        evidence_type=EvidenceType.USER_EXPLANATION,
        observation="TLB 是页表项的缓存，未命中还要查页表。",
        correctness=EvidenceCorrectness.CORRECT,
        sources=[EvidenceSourceItem(source_type="MESSAGE", source_id=7003)],
    )
    state = _state(
        message="我之前 TLB 错在哪？帮我复盘。",
        evidences=[_evidence(), explanation],
    )

    issues = check_history_answer("记录显示你在测试中答对了三道题。", state)

    assert any(issue.kind == "record_completeness" for issue in issues)


# ---------- B06：analyze 链路禁止 fixture 顶替真实检索 ----------


def test_b06_fixture_mode_is_rejected_when_degradation_disallowed() -> None:
    settings = Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        fixture_path=str(FIXTURE),
    )

    with pytest.raises(RetrievalError):
        build_retriever(settings, None, allow_degradation=False, user_id=9001)


def test_b06_analyze_fails_before_any_llm_call_when_fixture_bypassed() -> None:
    class CountingLLM:
        name = "counting"
        calls = 0

        async def stream_chat(self, messages, **kwargs):
            self.calls += 1
            yield "should not happen"

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return "should not happen"

        async def aclose(self):
            return None

    settings = Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        fixture_path=str(FIXTURE),
    )
    llm = CountingLLM()
    runtime = AgentRuntime(settings, llm)
    request = AnalyzeRequest(
        run_id=1,
        user_id=9001,
        conversation_id=9401,
        learning_space_id=9101,
        message_id=1,
        user_message="分析一下 TLB 的理解变化。",
        assistant_message="TLB 缓存页表项。",
    )

    with pytest.raises(RetrievalError):
        asyncio.run(runtime.analyze(request))
    assert llm.calls == 0, "检索保护失败时不得调用模型，否则可能产生会被写库的候选"


def test_b06_fixture_is_still_allowed_for_chat_when_degradation_allowed() -> None:
    settings = Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        fixture_path=str(FIXTURE),
    )

    retriever = build_retriever(settings, None, allow_degradation=True)
    try:
        assert retriever.name.startswith("fixture:")
    finally:
        asyncio.run(retriever.aclose())


# ---------- D07：auto 回退必须是结构化可观测事实 ----------


def test_d07_auto_without_credentials_exposes_fallback_metadata() -> None:
    settings = Settings(
        _env_file=None,
        retriever_mode="auto",
        llm_provider="mock",
        fixture_path=str(FIXTURE),
        max_context_tokens=3000,
        recent_history_limit=12,
    )
    runtime = AgentRuntime(settings, MockLLMAdapter())
    request = ChatRequest(
        run_id=1,
        user_id=9001,
        conversation_id=9401,
        learning_space_id=9101,
        user_message="TLB 是什么？",
    )

    prepared = asyncio.run(runtime.prepare(request))

    assert prepared.requested_mode == "auto"
    assert prepared.retriever_name.startswith("fixture:")
    assert prepared.degraded is True
    assert prepared.fallback_reason == "no_credentials"
    assert prepared.as_dict()["fallbackVisible"] is True


# ---------- E06：recent_history_limit=0 表示不注入历史 ----------


def test_e06_zero_history_limit_injects_no_history() -> None:
    history = [
        HistoryMessage(role=MessageRole.USER, content="先讲地址翻译。"),
        HistoryMessage(role=MessageRole.ASSISTANT, content="地址翻译从页表开始。"),
        HistoryMessage(role=MessageRole.USER, content="再说 TLB。"),
    ]
    context = ContextBuilder(max_context_tokens=3000, recent_history_limit=0).build(
        _state(history=history)
    )

    names = [section.name for section in context.sections]
    assert not any(name.startswith("history:") for name in names)
    assert len([message for message in context.messages if message.role in ("user", "assistant")]) == 1


def test_e06_negative_history_limit_is_invalid() -> None:
    with pytest.raises(ValueError):
        ContextBuilder(max_context_tokens=3000, recent_history_limit=-1)
