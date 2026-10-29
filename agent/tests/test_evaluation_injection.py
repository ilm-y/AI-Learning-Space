"""评测依赖注入与生产分析安全边界的回归测试。

背景：离线评测曾在所有非空消息用例上失败，因为执行器把
``AILS_RETRIEVER_MODE=fixture`` 交给生产 ``LearningAnalyzer``，而分析链路
会写长期认知，默认必须 ``allow_degradation=False``。

修复方式不是放宽生产限制，而是让评测显式传入一个固定的
``FixtureRetriever``。本文件钉住三件事：

1. 离线执行器确实能通过显式注入跑通 fixture 用例；
2. 不传 ``retriever`` 时，生产默认路径仍然拒绝 fixture 降级；
3. 注入只影响当前调用，不会改写 ``build_retriever`` 的选择或全局安全策略。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import AGENT_ROOT, Settings
from app.llm.mock import MockLLMAdapter
from app.retriever.base import RetrievalError
from app.retriever.fixture import FixtureRetriever
from app.runtime import AgentRuntime
from app.schemas import AnalyzeRequest
from evaluation.runner.models import load_offline_cases
from evaluation.runner.offline import run_offline_cases

FIXTURES_DIR = AGENT_ROOT / "fixtures"
DATASETS_DIR = REPO_ROOT / "evaluation" / "datasets"
CASES_DIR = REPO_ROOT / "evaluation" / "cases"
FIXTURE = FIXTURES_DIR / "demo_space.json"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        fixture_path=str(FIXTURE),
    )


def _request() -> AnalyzeRequest:
    return AnalyzeRequest(
        runId=6100,
        traceId="phase6-injection",
        userId=1,
        conversationId=1,
        learningSpaceId=1,
        messageId=6100,
        userMessage="我理解 TLB 就是页表项的缓存。",
        assistantMessage="是的，TLB 缓存页表项。",
    )


class _CountingLLM(MockLLMAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, messages, **kwargs):  # type: ignore[override]
        self.calls += 1
        return await super().complete(messages, **kwargs)


class _TrackingFixtureRetriever(FixtureRetriever):
    def __init__(self) -> None:
        super().__init__(FIXTURE)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_offline_evaluator_explicitly_injects_fixture_retriever() -> None:
    """两条此前会被生产保护拦下的用例，现在通过显式注入正常执行。"""

    cases = [
        case
        for case in load_offline_cases(CASES_DIR)
        if case.id in {"contract-fenced-json", "verification-new-concept-ask-later"}
    ]
    assert len(cases) == 2

    results = asyncio.run(
        run_offline_cases(
            cases,
            fixtures_dir=FIXTURES_DIR,
            datasets_dir=DATASETS_DIR,
        )
    )

    assert [result.status for result in results] == ["passed", "passed"]
    assert all(result.error is None for result in results)


def test_explicit_injection_does_not_change_default_analysis_security() -> None:
    """显式注入成功后，同一配置下不传 retriever 的生产路径仍然 fail closed。"""

    runtime = AgentRuntime(_settings(), MockLLMAdapter())
    injected = FixtureRetriever(FIXTURE)
    try:
        result = asyncio.run(runtime.analyze(_request(), retriever=injected))
        assert result.detected_concepts
    finally:
        asyncio.run(injected.aclose())
        asyncio.run(runtime.aclose())

    # 新 runtime、同一 fixture 配置；调用方没有注入，必须走默认安全分支。
    default_runtime = AgentRuntime(_settings(), MockLLMAdapter())
    try:
        with pytest.raises(RetrievalError):
            asyncio.run(default_runtime.analyze(_request()))
    finally:
        asyncio.run(default_runtime.aclose())


def test_default_analyze_rejects_fixture_before_llm_call() -> None:
    """生产默认分析路径不仅拒绝，还必须在调用模型前拒绝。"""

    llm = _CountingLLM()
    runtime = AgentRuntime(_settings(), llm)
    try:
        with pytest.raises(RetrievalError):
            asyncio.run(runtime.analyze(_request()))
    finally:
        asyncio.run(runtime.aclose())

    assert llm.calls == 0, "检索保护失败时不得调用模型，否则可能产生会被写库的候选"


def test_injected_retriever_is_used_without_falling_back_to_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入路径不得偷偷调用生产 ``build_retriever``；检索源归调用方关闭。"""

    import app.analysis.analyzer as analyzer_module

    def _unexpected_factory(*args, **kwargs):
        raise AssertionError("显式注入时不应再调用生产 build_retriever")

    monkeypatch.setattr(analyzer_module, "build_retriever", _unexpected_factory)

    retriever = _TrackingFixtureRetriever()
    runtime = AgentRuntime(_settings(), MockLLMAdapter())
    try:
        result = asyncio.run(runtime.analyze(_request(), retriever=retriever))
        assert result.detected_concepts
        assert retriever.closed is False, "调用方注入的检索源应由调用方关闭"
    finally:
        asyncio.run(retriever.aclose())
        asyncio.run(runtime.aclose())
