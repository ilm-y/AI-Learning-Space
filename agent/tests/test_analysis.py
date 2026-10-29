"""学习分析的契约测试。

两条路径都要覆盖：

1. **Mock 路径** —— 没有 API Key 时也必须产出可写入的确定性结果；
2. **结构化 LLM 路径** —— 模型输出不可信，必须在进入后端之前被裁剪与校验。

这里测的不是「模型判断得准不准」（那属于离线评测集），而是
「模型乱写时，会不会把错误数据写进长期理解」。
"""

from __future__ import annotations

import json

import pytest

from app.analysis import AnalysisError
from app.analysis.analyzer import ANALYZER_VERSION
from app.llm.base import ChatMessage
from app.llm.mock import MockLLMAdapter
from app.runtime import AgentRuntime
from app.schemas import (
    AnalyzeRequest,
    EvidenceCorrectness,
    EvidenceType,
    MemoryReconcileAction,
    MemoryScopeType,
    MemoryType,
    QuestionType,
    VerificationAction,
    VerificationTrigger,
)


@pytest.fixture(autouse=True)
def _inject_fixture_retriever_for_analysis(monkeypatch, request):
    """分析链路现在禁止 fixture 模式在生产配置里冒充真实检索（Phase 1.1 B06）。

    这些契约测试要验证的是分析器的解析/裁剪逻辑，不是检索保护本身，所以
    只在这个测试模块里显式注入 FixtureRetriever。保护行为由
    `test_analyze_never_falls_back_to_fixture` 和 Phase 1.1 回归单独覆盖。
    """
    if request.node.name == "test_analyze_never_falls_back_to_fixture":
        yield
        return
    from app.config import AGENT_ROOT
    from app.retriever.fixture import FixtureRetriever

    fixture = FixtureRetriever(AGENT_ROOT / "fixtures" / "demo_space.json")
    monkeypatch.setattr(
        "app.analysis.analyzer.build_retriever", lambda *args, **kwargs: fixture
    )
    yield


def make_analyze_request(message: str, *, history: list[tuple[str, str]] | None = None) -> AnalyzeRequest:
    from app.schemas import HistoryMessage, MessageRole

    return AnalyzeRequest(
        runId=2,
        traceId="trace-analyze",
        userId=1,
        conversationId=1,
        learningSpaceId=1,
        messageId=99,
        userMessage=message,
        assistantMessage="（助手回答）",
        history=[
            HistoryMessage(
                role=MessageRole.USER if role == "user" else MessageRole.ASSISTANT,
                content=content,
            )
            for role, content in (history or [])
        ],
    )


class _JsonLLM:
    """返回固定文本的假模型，用于验证解析与校验，不依赖任何网络。"""

    def __init__(self, payload: dict | str) -> None:
        self._payload = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake-json"

    async def stream_chat(self, messages: list[ChatMessage], **kwargs):
        raise AssertionError("分析路径不应调用 stream_chat")

    async def complete(self, messages: list[ChatMessage], **kwargs) -> str:
        self.calls += 1
        return self._payload

    async def aclose(self) -> None:
        return None


def mock_runtime(settings) -> AgentRuntime:
    """Mock 适配器的 name 是 "mock"，分析器据此走确定性启发式路径。"""
    return AgentRuntime(settings, MockLLMAdapter())


async def test_mock_analysis_returns_an_observation_for_the_detected_concept(settings):
    runtime = mock_runtime(settings)

    result = await runtime.analyze(make_analyze_request("TLB 是什么？"))

    assert result.detected_concepts == ["TLB（快表）"]
    assert len(result.evidence_candidates) == 1
    candidate = result.evidence_candidates[0]
    assert candidate.concept_id == 10
    assert candidate.evidence_type is EvidenceType.OBSERVATION
    assert candidate.correctness is EvidenceCorrectness.UNKNOWN
    assert candidate.need_follow_up is True


async def test_mock_analysis_turns_self_explanation_into_user_explanation(settings):
    runtime = mock_runtime(settings)

    result = await runtime.analyze(
        make_analyze_request("我觉得 TLB 就是 Page Table 的缓存。")
    )

    tlb = next(item for item in result.evidence_candidates if item.concept_id == 10)
    assert tlb.evidence_type is EvidenceType.USER_EXPLANATION
    assert tlb.correctness is EvidenceCorrectness.PARTIAL
    assert tlb.user_explanation == "我觉得 TLB 就是 Page Table 的缓存。"
    assert result.understanding_proposal is not None
    assert result.understanding_proposal.latest_user_explanation == (
        "我觉得 TLB 就是 Page Table 的缓存。"
    )


async def test_new_concept_question_is_deferred_not_interrupted(settings):
    runtime = mock_runtime(settings)

    result = await runtime.analyze(make_analyze_request("Cache 是什么？"))

    decision = result.verification_decision
    assert decision.action is VerificationAction.ASK_LATER
    assert decision.trigger is VerificationTrigger.NEW_CONCEPT
    assert decision.concept_id == 14
    assert decision.question_type is QuestionType.SHORT_ANSWER
    assert "高速缓存" in decision.question_text


async def test_known_concept_question_does_not_trigger_verification(settings):
    runtime = mock_runtime(settings)

    result = await runtime.analyze(make_analyze_request("TLB 是什么？"))

    assert result.verification_decision.action is VerificationAction.SKIP


async def test_stable_preference_becomes_a_global_memory_candidate(settings):
    runtime = mock_runtime(settings)

    result = await runtime.analyze(
        make_analyze_request("我喜欢先看硬件执行路径。TLB 是什么？")
    )

    assert len(result.memory_candidates) == 1
    memory = result.memory_candidates[0]
    assert memory.scope_type is MemoryScopeType.GLOBAL
    assert memory.memory_type is MemoryType.PREFERENCE
    assert memory.action is MemoryReconcileAction.ADD
    assert "我喜欢" in memory.content


async def test_structured_output_is_sanitized_before_it_reaches_the_backend(settings):
    """模型常见的三类越界：编造 conceptId、分数越界、ASK_NOW 却没有题目。"""
    payload = {
        "evidenceCandidates": [
            {
                "conceptId": 999,  # 不存在的概念：必须丢弃
                "evidenceType": "OBSERVATION",
                "observation": "编造的证据",
                "correctness": "CORRECT",
            },
            {
                "conceptId": 10,
                "evidenceType": "USER_EXPLANATION",
                "observation": "TLB 是页表项的缓存",
                "correctness": "CORRECT",
                "reasoningQuality": 1.8,  # 越界：裁剪到 1.0
                "specificity": -0.5,  # 越界：裁剪到 0.0
            },
        ],
        "understandingProposal": {
            "conceptId": 999,  # 不存在的 id：回落到本轮主概念
            "knownParts": ["TLB 是页表项的缓存", "TLB 是页表项的缓存"],  # 去重
        },
        "memoryCandidates": [],
        "verificationDecision": {
            "action": "ASK_NOW",
            "trigger": "OVERCONFIDENCE",
            "conceptId": 10,
            "reason": "用户说得很肯定，但证据只有一条",
            # 没有 questionText：必须补一道默认可作答的题
        },
    }
    runtime = AgentRuntime(settings, _JsonLLM(payload))

    result = await runtime.analyze(make_analyze_request("TLB 肯定就是页表。"))

    assert [item.concept_id for item in result.evidence_candidates] == [10]
    candidate = result.evidence_candidates[0]
    assert candidate.reasoning_quality == 1.0
    assert candidate.specificity == 0.0
    assert result.understanding_proposal is not None
    assert result.understanding_proposal.concept_id == 10
    assert result.understanding_proposal.known_parts == ["TLB 是页表项的缓存"]
    assert result.verification_decision.question_text
    assert result.verification_decision.question_type is QuestionType.SHORT_ANSWER
    assert result.analyzer_version == ANALYZER_VERSION


async def test_fenced_json_is_accepted(settings):
    """真实模型经常把 JSON 包在 Markdown 围栏里，这是允许的。"""
    text = """好的，分析如下：
```json
{"evidenceCandidates": [{"conceptId": 10, "observation": "TLB 是页表项缓存", "correctness": "CORRECT"}]}
```
"""
    runtime = AgentRuntime(settings, _JsonLLM(text))

    result = await runtime.analyze(make_analyze_request("TLB 是页表项缓存。"))

    assert len(result.evidence_candidates) == 1
    assert result.evidence_candidates[0].correctness is EvidenceCorrectness.CORRECT


async def test_prose_instead_of_json_fails_loudly(settings):
    """解析失败必须报错，让后端有记录地退回规则版 —— 不能假装「没有学习信号」。"""
    runtime = AgentRuntime(settings, _JsonLLM("这是一段普通回答，不是 JSON。"))

    with pytest.raises(AnalysisError):
        await runtime.analyze(make_analyze_request("TLB 是什么？"))


async def test_json_without_analysis_fields_fails_loudly(settings):
    """任意 JSON 对象都能通过 pydantic 默认值校验，所以必须显式检查字段。

    外层多包一层（`{"result": {...}}`）是模型最常见的漂移方式；如果这里放行，
    后端收到的是「成功但零候选」，不会退回 rules-v1，问题就被静默吞掉了。
    """
    payload = {"result": {"evidenceCandidates": [{"conceptId": 10}]}}
    runtime = AgentRuntime(settings, _JsonLLM(payload))

    with pytest.raises(AnalysisError, match="没有任何分析字段"):
        await runtime.analyze(make_analyze_request("TLB 是什么？"))


async def test_analyze_never_falls_back_to_fixture(settings):
    """分析结果会写进长期认知，auto 模式没有 token 时必须报错，不能用样例数据顶替。"""
    from app.retriever.base import RetrievalError

    auto = settings.model_copy(update={"retriever_mode": "auto"})
    runtime = mock_runtime(auto)

    with pytest.raises(RetrievalError, match="authToken"):
        await runtime.analyze(make_analyze_request("TLB 是什么？"))


async def test_no_concept_skips_the_model_entirely(settings):
    llm = _JsonLLM("should not be called")
    runtime = AgentRuntime(settings, llm)

    result = await runtime.analyze(make_analyze_request("今天天气怎么样？"))

    assert result.evidence_candidates == []
    assert result.understanding_proposal is None
    assert result.verification_decision.action is VerificationAction.SKIP
    assert llm.calls == 0
