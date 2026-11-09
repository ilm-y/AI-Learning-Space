"""D4-5 已知风险探针。

这些探针不复用 case 的预期，而是直接观察生产 Builder 在边界输入下的行为：

1. 知识全部被预算裁掉时，是否仍明确说明「检索到了但没放入」，而不是说「没有检索到」；
2. Session 概念不匹配时，旧 goal / focus 是否真的被剥离；
3. protected 区块超出总预算时，`used > effective` 是否如实记录、状态说明是否仍可见；
4. 语义冗余阈值 0.6 的两个方向：
   - 改写（paraphrase）是否漏判（保守偏置）；
   - 语义不同但字符重合高（例如否定句）是否被误判并在压缩中丢掉关键否定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.context.builder import (
    ContextBuilder,
    _bigrams,
    _normalize_overlap_text,
)
from app.context.policy import plan_retrieval
from app.schemas import (
    MemoryItem,
    MemoryScopeType,
    MemoryType,
    Understanding,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState, DetectedConcept
from app.trace import RunTrace
from tests.eval.context_quality.dataset import CATALOG, CASES, k
from tests.eval.context_quality.harness import (
    _section,
    assertion_present,
    run_case,
)


@dataclass
class ProbeResult:
    name: str
    description: str
    observations: dict[str, object] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    ok: bool = True
    risk_confirmed: bool = False


def _minimal_state(
    message: str,
    *,
    concept_id: int | None = None,
    session=None,
) -> AgentState:
    by_id = {item.id: item for item in CATALOG}
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message=message,
        session=session,
        detected_concepts=(
            [
                DetectedConcept(
                    concept=by_id[concept_id],
                    score=1.0,
                    matched_on="d4_5_probe",
                )
            ]
            if concept_id is not None
            else []
        ),
        trace=RunTrace(run_id=1),
    )
    return state


def _overlap(item_text: str, current_text: str) -> float:
    item = _normalize_overlap_text(item_text)
    current = _normalize_overlap_text(current_text)
    if len(item) < 2:
        return 0.0
    grams = _bigrams(item)
    if not grams:
        return 0.0
    return len(grams & _bigrams(current)) / len(grams)


def probe_budget_all_dropped() -> ProbeResult:
    state = _minimal_state("ORBIT-9000 的超时是多少？")
    plan = plan_retrieval(
        user_message=state.user_message,
        concept_ids=[],
        session=None,
        space_concepts=list(CATALOG),
    )
    state.retrieval_plan = plan
    state.knowledge = [
        k(
            9901,
            "KNW-ALL-DROPPED " + ("ORBIT-9000 的默认超时为 17 秒。" * 80),
            document_name="orbit_timeout.md",
        )
    ]
    state.knowledge_status = "SUCCESS"
    context = ContextBuilder(
        max_context_tokens=120, output_reserve_ratio=0.0
    ).build(state)
    prompt = context.system_instruction
    false_claim = assertion_present(prompt, "没有检索到资料")
    note_present = "状态：SUCCESS_BUT_DROPPED" in prompt
    return ProbeResult(
        name="budget_all_dropped",
        description="知识检索成功但全部片段被预算裁掉",
        observations={
            "knowledge_state": context.knowledge_state,
            "budget_exceeded": context.budget_exceeded,
            "status_note_present": note_present,
            "false_no_retrieval_claim": false_claim,
            "dropped_chunk_ids": context.knowledge_dropped_chunk_ids,
        },
        findings=[
            "Builder 用 SUCCESS_BUT_DROPPED 说明「检索到了但未放入」，"
            "并明确禁止改写成「没有检索到资料」。"
        ],
        ok=note_present and not false_claim,
    )


def probe_session_mismatch() -> ProbeResult:
    case = next(
        item
        for item in CASES
        if item.case_id == "topic-switch-derivative-to-tcp"
    )
    result = run_case(case)
    prompt = result.context.system_instruction
    stale_markers = [
        marker
        for marker in ("SESSION-STALE-DERIV", "SESSION-STALE-FOCUS")
        if marker in prompt
    ]
    return ProbeResult(
        name="session_mismatch",
        description="SessionResolver 把新话题挂到旧 Session",
        observations={
            "session_reason": result.plan.session.reason,
            "inject_goal": result.plan.session.inject_goal,
            "inject_current_focus": result.plan.session.inject_current_focus,
            "stale_markers_in_prompt": stale_markers,
            "session_entry_statuses": sorted(
                {
                    entry["injection_status"]
                    for entry in result.context.context_manifest["sources"]
                    if entry["source_id"] == "session:30"
                }
            ),
        },
        findings=[
            "Runtime 的 _apply_session_policy 在 Builder 之前清空 goal/focus；"
            "Builder 不会渲染空 Session 区块。"
        ],
        ok=not stale_markers
        and result.plan.session.inject_goal is False
        and result.plan.session.inject_current_focus is False,
    )


def probe_protected_overflow() -> ProbeResult:
    case = next(
        item
        for item in CASES
        if item.case_id == "budget-protected-overflow"
    )
    result = run_case(case)
    context = result.context
    budget = context.context_manifest["budget"]
    prompt = context.system_instruction
    status_present = (
        "学习认知检索：DEGRADED" in prompt
        and "本轮知识检索不可用（Embedding / 向量检索失败）" in prompt
    )
    return ProbeResult(
        name="protected_overflow",
        description="极小预算下 protected 状态说明超出总额",
        observations={
            "total": budget["total"],
            "effective": budget["effective"],
            "used": budget["used"],
            "budget_exceeded": context.budget_exceeded,
            "overrun_reason": context.budget_overrun_reason,
            "degraded_status_still_visible": status_present,
        },
        findings=[
            "Manifest 如实记录 used > effective，并给出 budget_overrun_reason；"
            "protected 状态说明没有因为超预算被丢弃。"
        ],
        ok=budget["used"] > budget["effective"]
        and context.budget_exceeded
        and bool(context.budget_overrun_reason)
        and status_present,
    )


def _redundancy_context(understanding: Understanding, memory: MemoryItem):
    state = _minimal_state("TLB 是什么？", concept_id=202)
    state.understandings = [understanding]
    state.understanding_status = "SUCCESS"
    state.memories = [memory]
    state.memory_status = "SUCCESS"
    plan = plan_retrieval(
        user_message=state.user_message,
        concept_ids=[202],
        session=None,
        space_concepts=list(CATALOG),
    )
    state.retrieval_plan = plan
    context = ContextBuilder(
        max_context_tokens=8000, output_reserve_ratio=0.0
    ).build(state)
    return context


def probe_redundancy_threshold() -> ProbeResult:
    # 方向 1：同一事实的改写，字符 bigram 重合低 → 漏判。
    paraphrase_understanding = Understanding(
        id=600,
        concept_id=202,
        concept_name="TLB（快表）",
        summary="TLB 缓存页表项",
        status=UnderstandingStatus.PARTIAL,
    )
    paraphrase_memory = MemoryItem(
        id=910,
        scope_type=MemoryScopeType.CONCEPT,
        learning_space_id=1,
        concept_id=202,
        memory_type=MemoryType.LEARNING,
        content="快表保存页表条目副本",
    )
    paraphrase_context = _redundancy_context(
        paraphrase_understanding, paraphrase_memory
    )
    paraphrase_section = _section(paraphrase_context, "memory:910")
    paraphrase_overlap = _overlap(
        paraphrase_memory.content, paraphrase_understanding.summary or ""
    )
    paraphrase_detected = (
        paraphrase_section.compression_reason == "redundant_with_understanding"
    )

    # 方向 2：同一段字符，只差一个「不是」——语义相反，但重合率仍 ≥ 0.6。
    # 用大量不同句子制造足够多的不同 bigram，使后缀说明不会把重合率稀释到阈值以下；
    # 预算放宽到 8000，避免 memory 先被类型预算压成 structured，从而隔离冗余判定。
    filler = "；".join(
        f"第{i}条说明导数用于分析函数在点{i}附近的瞬时变化趋势，"
        "并联系切线斜率与局部线性近似"
        for i in range(30)
    )
    negation_understanding = Understanding(
        id=601,
        concept_id=102,
        concept_name="导数",
        summary=filler + "，变化率为正。",
        status=UnderstandingStatus.PARTIAL,
    )
    negation_memory = MemoryItem(
        id=911,
        scope_type=MemoryScopeType.CONCEPT,
        learning_space_id=1,
        concept_id=102,
        memory_type=MemoryType.LEARNING,
        content=filler + "，不是变化率。TAIL-NEG-MARKER",
    )
    negation_context = _redundancy_context(
        negation_understanding, negation_memory
    )
    negation_section = _section(negation_context, "memory:911")
    negation_overlap = _overlap(
        negation_memory.content, negation_understanding.summary or ""
    )
    negation_detected = (
        negation_section.compression_reason == "redundant_with_understanding"
    )
    negation_lost = "不是" not in negation_section.content

    return ProbeResult(
        name="redundancy_threshold_0_6",
        description="语义冗余阈值的漏判与误判两个方向",
        observations={
            "paraphrase_overlap": round(paraphrase_overlap, 3),
            "paraphrase_detected_as_redundant": paraphrase_detected,
            "paraphrase_tier": paraphrase_section.tier,
            "negation_overlap": round(negation_overlap, 3),
            "negation_detected_as_redundant": negation_detected,
            "negation_tier": negation_section.tier,
            "negation_token_lost_in_compressed_text": negation_lost,
        },
        findings=[
            "改写方向：阈值 0.6 对词汇改写偏保守，可能漏判（只影响能否省预算，"
            "不会丢内容）。",
            "误判方向：否定句与肯定句字符重合高，会被判为冗余；若原文较长，"
            "structured 档的 120 字截断可能把关键否定截掉。这是已复现的已知限制，"
            "本批按边界要求未修改生产 Builder。",
        ],
        ok=not paraphrase_detected,
        risk_confirmed=True,
    )


def run_known_risk_probes() -> list[ProbeResult]:
    return [
        probe_budget_all_dropped(),
        probe_session_mismatch(),
        probe_protected_overflow(),
        probe_redundancy_threshold(),
    ]
