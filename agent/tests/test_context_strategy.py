"""A/B/C 上下文策略开关：启动期安全约束与 Runtime 分支回归。

覆盖四类事实：

1. 默认策略是 C，生产配置（未声明隔离测试）拒绝 A/B；
2. 完整隔离测试配置允许 A/B，缺任一条即 fail closed；
3. A/B 的检索与注入差异符合 §2.7.3 的三层近似定义；
4. C 策略在开关前后逐字段一致——golden 在实现开关**之前**生成，
   对比时只剔除新增的 ``contextStrategy`` trace 字段。

这些测试不连 MySQL / Qdrant / 真实模型。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import ChatRequest, Concept, SessionContext
from tests.context_strategy_fixtures import (
    ENV_ID,
    DeterministicRetriever,
    collect_payload,
    make_request,
    make_settings,
    payload_json,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "context_strategy_c_golden.json"
DELEGATION_SECRET = "phase5-strategy-test-only-delegation-secret"


@pytest.fixture(autouse=True)
def _clear_strategy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽开发机环境变量，保证每个用例只验证显式传入的配置。"""
    for key in (
        "AILS_CONTEXT_STRATEGY",
        "AILS_TEST_ONLY",
        "AILS_TEST_ENV_ID",
        "AILS_QDRANT_COLLECTION_PREFIX",
        "AILS_MEMORY_COLLECTION_PREFIX",
        "AILS_HOST",
        "AILS_INTERNAL_DELEGATION_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def _isolated(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "context_strategy": "A",
        "test_only": True,
        "test_env_id": ENV_ID,
        "qdrant_collection_prefix": ENV_ID,
        "memory_collection_prefix": f"{ENV_ID}-memory",
        "host": "127.0.0.1",
    }
    values.update(overrides)
    return Settings(**values)


# ===================== 启动期安全约束 =====================


def test_default_strategy_is_c() -> None:
    settings = Settings(_env_file=None)

    assert settings.context_strategy == "C"
    assert settings.test_only is False
    assert settings.test_env_id == ""


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_production_rejects_experimental_strategy(strategy: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, context_strategy=strategy)

    message = str(excinfo.value)
    assert "AILS_TEST_ONLY" in message
    assert "AILS_TEST_ENV_ID" in message


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_test_only_without_valid_env_id_is_still_rejected(strategy: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            context_strategy=strategy,
            test_only=True,
            test_env_id="production",
        )

    assert "AILS_TEST_ENV_ID" in str(excinfo.value)


@pytest.mark.parametrize(
    "prefix",
    [
        "ails_documents",
        # 以 env_id 开头但追加了后缀：旧的 startswith 会放行，必须拒绝。
        f"{ENV_ID}-other",
        f"{ENV_ID}0",
        f"{ENV_ID}_suffix",
    ],
)
def test_collection_prefix_must_equal_env_id(prefix: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _isolated(qdrant_collection_prefix=prefix)

    message = str(excinfo.value)
    assert "AILS_QDRANT_COLLECTION_PREFIX" in message
    assert "AILS_MEMORY_COLLECTION_PREFIX" not in message


@pytest.mark.parametrize(
    "prefix",
    [
        "ails_memories",
        # 以 env_id 开头但不是 `<env_id>-memory`：旧的 startswith 会放行。
        ENV_ID,
        f"{ENV_ID}-memory-extra",
        f"{ENV_ID}-other",
    ],
)
def test_memory_prefix_must_equal_env_id_plus_memory(prefix: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _isolated(memory_collection_prefix=prefix)

    assert "AILS_MEMORY_COLLECTION_PREFIX" in str(excinfo.value)


def test_exact_prefix_pair_is_accepted() -> None:
    settings = _isolated(
        qdrant_collection_prefix=ENV_ID,
        memory_collection_prefix=f"{ENV_ID}-memory",
    )

    assert settings.qdrant_collection_prefix == ENV_ID
    assert settings.memory_collection_prefix == f"{ENV_ID}-memory"


def test_non_loopback_host_is_rejected_for_experimental_strategy() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _isolated(host="0.0.0.0")

    assert "AILS_HOST" in str(excinfo.value)


@pytest.mark.parametrize("strategy", ["A", "B"])
def test_complete_isolated_configuration_allows_experimental_strategy(
    strategy: str,
) -> None:
    settings = _isolated(context_strategy=strategy)

    assert settings.context_strategy == strategy


def test_c_is_allowed_in_production_configuration() -> None:
    settings = Settings(_env_file=None, context_strategy="C")

    assert settings.context_strategy == "C"


def test_delegation_secret_guard_is_unconditional_not_strategy_specific() -> None:
    """细节 2：delegation secret 为空是字段级全局不变量，不是 A/B 的条件。

    即使隔离测试配置全部合法，只要 Agent 配置里出现该密钥，启动仍然失败。
    策略校验器不重复这条条件（重复只会是恒真的死代码）。
    """
    with pytest.raises(ValidationError) as excinfo:
        _isolated(internal_delegation_secret=DELEGATION_SECRET)

    message = str(excinfo.value)
    assert "AILS_INTERNAL_DELEGATION_SECRET" in message
    assert DELEGATION_SECRET not in message


def test_describe_reports_strategy() -> None:
    described = _isolated().describe()

    assert described["context_strategy"] == "A"
    assert described["test_only"] is True
    # 守卫字段仍然不出现在 /health 输出里。
    assert "internal_delegation_secret" not in described


# ===================== Runtime 分支 =====================


async def test_a_keeps_knowledge_but_drops_all_personal_context() -> None:
    payload = await collect_payload("A")

    assert payload["retrieverCalls"] == ["concepts", "knowledge"]
    assert payload["historyMessages"] == 0
    assert payload["sessionPresent"] is False
    assert payload["retrievalPlan"] is None
    assert payload["statuses"] == {
        "understanding": "NOT_QUERIED",
        "memory": "NOT_QUERIED",
        "evidence": "NOT_RETRIEVED",
        "revision": "NOT_QUERIED",
        "knowledge": "SUCCESS",
    }
    assert [section.split(":")[0] for section in payload["sectionNames"]] == [
        "user_input",
        "knowledge",
    ]
    source_types = {source["type"] for source in payload["manifest"]["sources"]}
    assert source_types == {"user_input", "knowledge"}
    assert payload["citations"] == [
        {
            "documentId": 801,
            "chunkId": 701,
            "chunkIndex": 0,
            "sourceTitle": "tcp_handshake.md",
        }
    ]


async def test_b_forces_all_sources_and_has_no_retrieval_plan() -> None:
    retriever = DeterministicRetriever(degraded=True)
    payload = await collect_payload("B", retriever=retriever)

    assert payload["retrieverCalls"] == [
        "concepts",
        "knowledge",
        "understandings",
        "memories",
        "revisions",
        "evidence_details",
    ]
    assert payload["historyMessages"] == 2
    assert payload["sessionPresent"] is True
    assert payload["retrievalPlan"] is None
    # DEGRADED 在进入 Builder 前折叠为 EMPTY（B 的实验近似）。
    assert payload["statuses"]["understanding"] == "EMPTY"
    assert payload["statuses"]["memory"] == "EMPTY"
    assert payload["statuses"]["revision"] == "EMPTY"
    # Knowledge 不折叠：文档检索失败必须对模型可见。
    assert payload["statuses"]["knowledge"] == "SUCCESS"
    source_types = {source["type"] for source in payload["manifest"]["sources"]}
    assert {
        "user_input",
        "history",
        "session",
        "understanding",
        "memory",
        "knowledge",
        "trajectory",
        "trajectory_source",
    } <= source_types


async def test_b_marks_simulated_approximation_in_retrieve_trace() -> None:
    """三层定义第 3 层：模拟的 DEGRADED→EMPTY 必须在结果元数据显式标注。"""
    for strategy, expected in (
        ("B", "simulated-degraded-as-empty"),
        ("A", None),
        ("C", None),
    ):
        payload = await collect_payload(strategy)
        retrieve = next(
            event for event in payload["traceEvents"] if event["type"] == "RETRIEVE"
        )
        assert retrieve["detail"].get("bApproximation") == expected


async def test_b_skips_session_policy_while_c_applies_it() -> None:
    """Session 与本轮概念不相交时，C 清空 goal/focus，B 不做这层过滤。"""
    request = make_request().model_copy(
        update={
            "session": SessionContext(
                id=5,
                title="操作系统",
                goal="理解进程调度",
                current_focus="时间片轮转",
            )
        }
    )

    # Space 里同时有 TCP 与「操作系统」；Session 文本只提到后者，
    # 与本轮识别出的 TCP 完全不相交，C 的 Session policy 才会清空 goal/focus。
    retriever = DeterministicRetriever(
        concepts=[
            Concept(id=1, canonical_name="TCP 三次握手"),
            Concept(id=2, canonical_name="操作系统"),
        ]
    )
    b_state = (await _prepare("B", request, retriever)).state
    c_state = (await _prepare("C", request, retriever)).state

    assert b_state.session is not None
    assert b_state.session.goal == "理解进程调度"
    assert b_state.session.current_focus == "时间片轮转"
    assert c_state.session is not None
    assert c_state.session.goal is None
    assert c_state.session.current_focus is None


async def _prepare(
    strategy: str,
    request: ChatRequest,
    retriever: DeterministicRetriever | None = None,
):
    from app.runtime import AgentRuntime
    from app.trace import RunTrace
    from tests.context_strategy_fixtures import FakeLLM

    runtime = AgentRuntime(make_settings(strategy), FakeLLM())
    return await runtime.prepare(
        request,
        RunTrace(run_id=request.run_id, trace_id=request.trace_id),
        retriever=retriever or DeterministicRetriever(),
    )


async def test_every_run_records_context_strategy() -> None:
    for strategy in ("A", "B", "C"):
        payload = await collect_payload(strategy)
        start = next(
            event for event in payload["traceEvents"] if event["type"] == "RUN_START"
        )
        retrieve = next(
            event for event in payload["traceEvents"] if event["type"] == "RETRIEVE"
        )
        assert start["detail"]["contextStrategy"] == strategy
        assert retrieve["detail"]["contextStrategy"] == strategy


# ===================== C 路径回归 =====================


async def test_c_strategy_matches_pre_switch_golden() -> None:
    """C 的 Manifest / Citation / 状态与开关前 golden 完全一致。

    golden 由实现开关前的代码生成；唯一允许的差异是新增的
    ``contextStrategy`` trace 字段（已在本用例中剥离并单独断言）。
    """
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = json.loads(payload_json(await collect_payload("C")))

    recorded: list[str] = []
    for event in actual["traceEvents"]:
        value = event["detail"].pop("contextStrategy", None)
        if value is not None:
            recorded.append(value)

    assert actual == golden
    assert recorded == ["C", "C"]
