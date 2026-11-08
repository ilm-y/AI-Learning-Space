"""E2 离线评测执行器：规则 / 规则 + E1 / 规则 + Resolver + E1。

**诚实声明**：这里的 Resolver 输出由 ScriptedEvalLLM 按用例预置，不是真实模型。
它证明的是「管线有没有正确接入与校验」，不是「模型识别得准不准」。
所有 Token 数都是 `estimate_tokens` 的估算值（`token_source=estimated`），
延迟是 `prepare()` 的墙钟耗时，不包含真实网络 / 模型延迟。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from app.config import Settings
from app.llm.base import ChatMessage
from app.runtime import AgentRuntime
from app.schemas import HistoryMessage, MessageRole
from tests.eval.fake_world import FakeEvalRetriever

REPORTS_DIR = Path(__file__).resolve().parent / "reports"

MODE_RULES = "rules_only"
MODE_RULES_E1 = "rules_plus_e1"
MODE_RESOLVER_E1 = "rules_plus_resolver_plus_e1"
MODES = (MODE_RULES, MODE_RULES_E1, MODE_RESOLVER_E1)

MODE_LABELS = {
    MODE_RULES: "仅规则识别（无 E1、无 Resolver）",
    MODE_RULES_E1: "规则识别 + E1 关系扩展",
    MODE_RESOLVER_E1: "规则识别 + Resolver + E1 关系扩展",
}


def _json(
    *,
    current: list[tuple[int, float]] | None = None,
    related: list[tuple[int, float]] | None = None,
    needs_history: bool = False,
    intent: str = "knowledge_question",
) -> str:
    def rows(items: list[tuple[int, float]]) -> list[dict[str, object]]:
        return [
            {"conceptId": concept_id, "confidence": confidence, "reason": "脚本化评测输出"}
            for concept_id, confidence in items
        ]

    return json.dumps(
        {
            "current_concepts": rows(current or []),
            "related_concepts": rows(related or []),
            "needs_history_reference": needs_history,
            "intent": intent,
        },
        ensure_ascii=False,
    )


SCRIPTED_RESPONSES: dict[str, str] = {
    "导数的前置是什么？依据是什么": _json(
        current=[(102, 0.95)], related=[(101, 0.8)], intent="prerequisite_question"
    ),
    "连续性的基础是什么？凭什么": _json(
        current=[(103, 0.9)], related=[(101, 0.75)], intent="prerequisite_question"
    ),
    "拥塞控制的前置是什么？为什么": _json(
        current=[(302, 0.92)], related=[(301, 0.85)], intent="prerequisite_question"
    ),
    "页表的基础是什么？凭什么": _json(
        current=[(201, 0.9)], related=[(202, 0.7)], intent="prerequisite_question"
    ),
    "求导前面的基础是什么？为什么": _json(
        current=[(102, 0.88)], related=[(101, 0.7)], intent="prerequisite_question"
    ),
    "那个东西到底是什么？": _json(
        current=[(101, 0.7)], needs_history=True, intent="knowledge_question"
    ),
    # 故意让模型把 Space 9 的同名「极限」(901) 也带回来：
    # 服务端必须按当前 Space 候选集丢弃它。
    "趋近的基础是什么？为什么": _json(
        current=[(101, 0.9), (901, 0.8)], intent="prerequisite_question"
    ),
}


class ScriptedEvalLLM:
    """按当前用户消息返回预置 JSON；未配置的问题返回合法空结果。"""

    name = "scripted-eval"

    def __init__(self) -> None:
        self.complete_calls: list[str] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "评测回答"

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        prompt = messages[-1].content if messages else ""
        self.complete_calls.append(prompt)
        for question, payload in SCRIPTED_RESPONSES.items():
            if question in prompt:
                return payload
        return _json(intent="other")

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    question: str
    space_id: int
    gold_current: frozenset[int]
    gold_related: frozenset[int]
    expected: dict[str, dict[str, object]]
    history: tuple[tuple[str, str], ...] = ()
    forbidden: frozenset[int] = frozenset()

    @property
    def gold(self) -> frozenset[int]:
        return self.gold_current | self.gold_related


def _expected(
    *,
    rules_current: list[int],
    rules_expanded: list[int],
    resolver_current: list[int],
    resolver_expanded: list[int],
    resolver_called: bool,
    needs_history: bool = False,
) -> dict[str, dict[str, object]]:
    return {
        MODE_RULES: {
            "current": rules_current,
            "expanded": [],
            "resolverCalled": False,
            "needsHistory": False,
        },
        MODE_RULES_E1: {
            "current": rules_current,
            "expanded": rules_expanded,
            "resolverCalled": False,
            "needsHistory": False,
        },
        MODE_RESOLVER_E1: {
            "current": resolver_current,
            "expanded": resolver_expanded,
            "resolverCalled": resolver_called,
            "needsHistory": needs_history,
        },
    }


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="prereq-derivative-limit",
        question="导数的前置是什么？依据是什么",
        space_id=1,
        gold_current=frozenset({102}),
        gold_related=frozenset({101}),
        expected=_expected(
            rules_current=[102],
            rules_expanded=[101],
            resolver_current=[102],
            resolver_expanded=[101],
            resolver_called=True,
        ),
    ),
    EvalCase(
        case_id="related-continuity-limit",
        question="连续性的基础是什么？凭什么",
        space_id=1,
        gold_current=frozenset({103}),
        gold_related=frozenset({101}),
        expected=_expected(
            rules_current=[103],
            rules_expanded=[101],
            resolver_current=[103],
            resolver_expanded=[101],
            resolver_called=True,
        ),
    ),
    EvalCase(
        case_id="tcp-congestion",
        question="拥塞控制的前置是什么？为什么",
        space_id=3,
        gold_current=frozenset({302}),
        gold_related=frozenset({301}),
        expected=_expected(
            rules_current=[302],
            rules_expanded=[301],
            resolver_current=[302],
            resolver_expanded=[301],
            resolver_called=True,
        ),
    ),
    EvalCase(
        case_id="pagetable-tlb",
        question="页表的基础是什么？凭什么",
        space_id=2,
        gold_current=frozenset({201}),
        gold_related=frozenset({202}),
        expected=_expected(
            rules_current=[201],
            rules_expanded=[202],
            resolver_current=[201],
            resolver_expanded=[202],
            resolver_called=True,
        ),
    ),
    EvalCase(
        case_id="synonym-derivative-basics",
        question="求导前面的基础是什么？为什么",
        space_id=1,
        gold_current=frozenset({102}),
        gold_related=frozenset({101}),
        expected=_expected(
            rules_current=[],
            rules_expanded=[],
            resolver_current=[102],
            resolver_expanded=[101],
            resolver_called=True,
        ),
    ),
    EvalCase(
        case_id="fuzzy-anaphora",
        question="那个东西到底是什么？",
        space_id=1,
        history=(
            ("user", "我又把刚才那个过程搞混了"),
            ("assistant", "你说的是趋近过程吧？"),
        ),
        gold_current=frozenset({101}),
        gold_related=frozenset(),
        expected=_expected(
            rules_current=[],
            rules_expanded=[],
            resolver_current=[101],
            resolver_expanded=[],
            resolver_called=True,
            needs_history=True,
        ),
    ),
    EvalCase(
        case_id="cross-space-isolation",
        question="趋近的基础是什么？为什么",
        space_id=1,
        gold_current=frozenset({101}),
        gold_related=frozenset(),
        forbidden=frozenset({901}),
        expected=_expected(
            rules_current=[],
            rules_expanded=[],
            resolver_current=[101],
            resolver_expanded=[],
            resolver_called=True,
        ),
    ),
    EvalCase(
        case_id="chitchat-short-message",
        question="你好",
        space_id=1,
        gold_current=frozenset(),
        gold_related=frozenset(),
        expected=_expected(
            rules_current=[],
            rules_expanded=[],
            resolver_current=[],
            resolver_expanded=[],
            resolver_called=False,
        ),
    ),
    EvalCase(
        case_id="rule-hit-no-resolver",
        question="导数的定义是什么",
        space_id=1,
        gold_current=frozenset({102}),
        gold_related=frozenset(),
        expected=_expected(
            rules_current=[102],
            rules_expanded=[],
            resolver_current=[102],
            resolver_expanded=[],
            resolver_called=False,
        ),
    ),
)


def _mode_settings(mode: str) -> Settings:
    if mode not in MODES:
        raise ValueError(f"未知评测模式：{mode}")
    return Settings(
        _env_file=None,
        llm_provider="mock",
        retriever_mode="fixture",
        max_context_tokens=3000,
        # 关闭 Resolver 时必须完全退化为 E1 行为；两个模式同时验证这一点。
        concept_resolver_enabled=mode == MODE_RESOLVER_E1,
        # 只有 rules_only 关闭 E1；另外两个模式都打开。
        evidence_related_concepts_limit=0 if mode == MODE_RULES else 3,
        evidence_related_evidence_limit=2,
    )


def _history(case: EvalCase) -> list[HistoryMessage]:
    return [
        HistoryMessage(
            role=MessageRole.USER if role == "user" else MessageRole.ASSISTANT,
            content=content,
        )
        for role, content in case.history
    ]


@dataclass
class RunResult:
    case: EvalCase
    mode: str
    current: list[int]
    expanded: list[int]
    resolver_status: str
    resolver_called: bool
    needs_history: bool
    prompt_tokens: int
    completion_tokens: int
    token_source: str
    resolver_elapsed_ms: int
    elapsed_ms: float
    llm_calls: int
    failures: list[str]

    @property
    def final(self) -> set[int]:
        return set(self.current) | set(self.expanded)

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "caseId": self.case.case_id,
            "question": self.case.question,
            "spaceId": self.case.space_id,
            "gold": sorted(self.case.gold),
            "observed": {
                "current": self.current,
                "expanded": self.expanded,
                "final": sorted(self.final),
                "resolverStatus": self.resolver_status,
                "resolverCalled": self.resolver_called,
                "needsHistoryReference": self.needs_history,
                "resolverPromptTokens": self.prompt_tokens,
                "resolverCompletionTokens": self.completion_tokens,
                "resolverTokenSource": self.token_source,
                "resolverElapsedMs": self.resolver_elapsed_ms,
                "prepareElapsedMs": round(self.elapsed_ms, 3),
                "llmCalls": self.llm_calls,
            },
            "passed": self.passed,
            "failures": self.failures,
        }


def _assertions(
    case: EvalCase,
    mode: str,
    *,
    current: list[int],
    expanded: list[int],
    resolver_called: bool,
    needs_history: bool,
    llm_calls: int,
) -> list[str]:
    expected = case.expected[mode]
    failures: list[str] = []
    if set(current) != set(expected["current"]):
        failures.append(
            f"current={sorted(current)} 期望 {sorted(expected['current'])}"
        )
    if set(expanded) != set(expected["expanded"]):
        failures.append(
            f"expanded={sorted(expanded)} 期望 {sorted(expected['expanded'])}"
        )
    if resolver_called != bool(expected["resolverCalled"]):
        failures.append(
            f"resolverCalled={resolver_called} 期望 {expected['resolverCalled']}"
        )
    if needs_history != bool(expected["needsHistory"]):
        failures.append(
            f"needsHistoryReference={needs_history} 期望 {expected['needsHistory']}"
        )
    if len(current) != len(set(current)):
        failures.append(f"current 存在重复 conceptId：{current}")
    forbidden_hits = set(current) | set(expanded)
    forbidden_hits &= case.forbidden
    if forbidden_hits:
        failures.append(f"出现禁止的 conceptId：{sorted(forbidden_hits)}")
    if llm_calls > 1:
        failures.append(f"单轮 Resolver 调用 {llm_calls} 次，超过上限 1")
    return failures


async def _run_case(case: EvalCase, mode: str) -> RunResult:
    settings = _mode_settings(mode)
    llm = ScriptedEvalLLM()
    runtime = AgentRuntime(settings, llm)
    retriever = FakeEvalRetriever(case.space_id)
    request = _make_request(case)
    started = time.perf_counter()
    prepared = await runtime.prepare(request, retriever=retriever)
    elapsed_ms = (time.perf_counter() - started) * 1000
    state = prepared.state
    current = list(state.concept_ids)
    expanded = [item.concept_id for item in state.expanded_concepts]
    resolver_called = state.resolver_status != "NOT_CALLED"
    failures = _assertions(
        case,
        mode,
        current=current,
        expanded=expanded,
        resolver_called=resolver_called,
        needs_history=state.resolver_needs_history_reference,
        llm_calls=len(llm.complete_calls),
    )
    return RunResult(
        case=case,
        mode=mode,
        current=current,
        expanded=expanded,
        resolver_status=state.resolver_status,
        resolver_called=resolver_called,
        needs_history=state.resolver_needs_history_reference,
        prompt_tokens=state.resolver_prompt_tokens,
        completion_tokens=state.resolver_completion_tokens,
        token_source=state.resolver_token_source,
        resolver_elapsed_ms=state.resolver_elapsed_ms,
        elapsed_ms=elapsed_ms,
        llm_calls=len(llm.complete_calls),
        failures=failures,
    )


def _make_request(case: EvalCase):
    # 放在函数里导入，避免评测模块在 import 时绑定请求模型细节。
    from app.schemas import ChatRequest

    return ChatRequest(
        runId=1,
        userId=1,
        conversationId=1,
        learningSpaceId=case.space_id,
        userMessage=case.question,
        history=_history(case),
    )


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _metrics(runs: list[RunResult]) -> dict[str, object]:
    tp = fp = fn = 0
    latencies: list[float] = []
    calls = 0
    token_total = 0
    resolver_latencies: list[int] = []
    token_sources: set[str] = set()
    for run in runs:
        final = run.final
        gold = set(run.case.gold)
        tp += len(final & gold)
        fp += len(final - gold)
        fn += len(gold - final)
        latencies.append(run.elapsed_ms)
        if run.resolver_called:
            calls += 1
            token_total += run.prompt_tokens + run.completion_tokens
            resolver_latencies.append(run.resolver_elapsed_ms)
            token_sources.add(run.token_source)
    recall = tp / (tp + fn) if tp + fn else 1.0
    precision = tp / (tp + fp) if tp + fp else 1.0
    return {
        "caseRuns": len(runs),
        "truePositives": tp,
        "falsePositives": fp,
        "falseNegatives": fn,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "misrecallRate": round(fp / (tp + fp), 4) if tp + fp else 0.0,
        "p95PrepareLatencyMs": round(_p95(latencies), 3),
        "avgPrepareLatencyMs": round(sum(latencies) / len(latencies), 3)
        if latencies
        else 0.0,
        "resolverCalls": calls,
        "resolverCallRate": round(calls / len(runs), 4) if runs else 0.0,
        "avgResolverTokens": round(token_total / calls, 2) if calls else 0,
        "totalResolverTokens": token_total,
        "avgResolverLatencyMs": round(sum(resolver_latencies) / len(resolver_latencies), 3)
        if resolver_latencies
        else 0.0,
        "tokenSources": sorted(token_sources),
    }


def run_mode(mode: str, *, repeats: int = 3) -> dict[str, object]:
    runs: list[RunResult] = []
    for _ in range(max(1, repeats)):
        for case in CASES:
            runs.append(asyncio.run(_run_case(case, mode)))

    first_pass = {run.case.case_id: run for run in reversed(runs)}
    ordered_first = [first_pass[case.case_id] for case in CASES]
    return {
        "mode": mode,
        "label": MODE_LABELS[mode],
        "passed": all(run.passed for run in runs),
        "metrics": _metrics(runs),
        "cases": [run.as_dict() for run in ordered_first],
    }


def run_eval(*, repeats: int = 3, write_report: bool = False) -> dict[str, object]:
    report: dict[str, object] = {
        "meta": {
            "experiment": "E2 Concept Resolver 三模式离线对照",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "runId": uuid.uuid4().hex,
            "repeats": repeats,
            "dataset": "agent/tests/eval/runner.py::CASES (9 cases)",
            "llm": "ScriptedEvalLLM（预置 JSON，不是真实模型）",
            "tokenAccounting": "estimate_tokens 估算值（token_source=estimated）",
            "latencyCaveat": (
                "p95/avg 是 prepare() 墙钟耗时；脚本化 LLM 不产生真实模型/网络延迟，"
                "因此不同模式之间的延迟差只反映管线路径（Resolver 调用 + E1 查询）。"
            ),
            "goldDefinition": (
                "gold = 用户实际指向的当前概念 ∪ 应被 E1 带出的相关概念；"
                "Recall/Precision 在 final = current ∪ expanded 上按概念 id 计算。"
            ),
        },
        "modes": {},
    }
    modes: dict[str, object] = report["modes"]  # type: ignore[assignment]
    for mode in MODES:
        modes[mode] = run_mode(mode, repeats=repeats)
    report["summary"] = {
        "allPassed": all(result["passed"] for result in modes.values()),
        "modeOrder": list(MODES),
    }
    if write_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = REPORTS_DIR / f"{stamp}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["reportPath"] = str(path)
    return report


def deterministic_projection(report: dict[str, object]) -> dict[str, object]:
    """去掉时间/延迟后的投影，用来证明同一数据集可重复。"""
    projection: dict[str, object] = {}
    for mode, result in report["modes"].items():  # type: ignore[union-attr]
        metrics = result["metrics"]
        projection[mode] = {
            "metrics": {
                key: metrics[key]
                for key in (
                    "recall",
                    "precision",
                    "misrecallRate",
                    "resolverCalls",
                    "resolverCallRate",
                    "avgResolverTokens",
                    "totalResolverTokens",
                )
            },
            "cases": [
                {
                    "caseId": case["caseId"],
                    "current": case["observed"]["current"],
                    "expanded": case["observed"]["expanded"],
                    "resolverStatus": case["observed"]["resolverStatus"],
                    "resolverCalled": case["observed"]["resolverCalled"],
                    "needsHistoryReference": case["observed"]["needsHistoryReference"],
                    "llmCalls": case["observed"]["llmCalls"],
                }
                for case in result["cases"]
            ],
        }
    return projection
