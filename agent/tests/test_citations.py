"""引用校验器的离线测试。

这里只验证**可机器判定**的部分：标注是否与本轮上下文精确对应。
`lexical_support` 是启发式，只断言它不把明显无关的句子判成 strong，
不把它当事实正确性测试。
"""

from __future__ import annotations

from app.citations import (
    format_source_label,
    provided_labels_from_context,
    validate_citations,
)
from app.context.builder import BuiltContext, ContextBuilder, ContextSection
from app.schemas import KnowledgeChunk
from app.state.agent_state import AgentState
from app.trace import RunTrace


def chunk(
    *,
    chunk_id: int = 1,
    document_id: int = 10,
    chunk_index: int = 0,
    name: str = "zenframe.md",
    content: str = "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。",
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id,
        document_id=document_id,
        chunk_index=chunk_index,
        document_name=name,
        content=content,
    )


def test_format_source_label_is_the_single_source_of_truth() -> None:
    assert format_source_label(chunk()) == "资料《zenframe.md》#0"
    assert format_source_label(chunk(chunk_index=None)) == "资料《zenframe.md》片段#1"
    assert format_source_label(chunk(name="")) == "资料片段#1"


def test_valid_citation_is_matched_exactly() -> None:
    answer = "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒 [资料《zenframe.md》#0]。"
    report = validate_citations(answer, [chunk()])

    assert report.total == 1
    assert report.valid == 1
    assert report.invalid == 0
    assert report.illegal_rate == 0
    assert report.cited_documents == ["zenframe.md"]
    citation = report.citations[0]
    assert citation.valid is True
    assert citation.chunk_id == 1
    assert citation.lexical_support in {"strong", "weak"}
    assert "MQTT" in citation.source_excerpt


def test_unknown_document_and_unknown_chunk_index_are_both_illegal() -> None:
    answer = (
        "说法一 [资料《zealdeck.md》#0]，"
        "说法二 [资料《zenframe.md》#1]。"
    )
    report = validate_citations(answer, [chunk()])

    assert report.total == 2
    assert report.valid == 0
    assert report.invalid == 2
    assert report.illegal_rate == 1
    assert set(report.invalid_labels) == {
        "[资料《zealdeck.md》#0]",
        "[资料《zenframe.md》#1]",
    }


def test_citation_to_a_chunk_dropped_from_context_is_illegal() -> None:
    # 检索到了，但 Token 预算把它丢掉：模型看不到，也不能合法引用。
    contexts = BuiltContext(
        system_instruction="",
        messages=[],
        sections=[
            ContextSection(
                name="knowledge:1",
                title="参考资料",
                content="- [资料《zenframe.md》#0] ZenFrame 使用 MQTT。",
                priority=40,
                source_ids=["chunk:1"],
                dropped=True,
            )
        ],
        estimated_tokens=0,
    )
    labels = provided_labels_from_context(contexts, [chunk()])
    report = validate_citations(
        "ZenFrame 使用 MQTT [资料《zenframe.md》#0]。",
        [chunk()],
        provided_labels=labels,
    )

    assert labels == []
    assert report.invalid == 1
    assert report.invalid_labels == ["[资料《zenframe.md》#0]"]


def test_context_builder_labels_are_accepted_by_validator() -> None:
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message="ZenFrame 的提醒模块通过什么协议？",
    )
    state.knowledge = [chunk()]
    state.trace = RunTrace(run_id=1)
    context = ContextBuilder(max_context_tokens=3000).build(state)
    labels = provided_labels_from_context(context, state.knowledge)

    assert labels == ["资料《zenframe.md》#0"]
    report = validate_citations(
        "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒 [资料《zenframe.md》#0]。",
        state.knowledge,
        provided_labels=labels,
    )
    assert report.valid == 1
    assert report.invalid == 0


def test_duplicate_and_malformed_labels_are_reported_separately() -> None:
    answer = (
        "事实 [资料《zenframe.md》#0]，重复 [资料《zenframe.md》#0]，"
        "坏格式 [资料《zenframe.md》] 和 [资料 zenframe]"
    )
    report = validate_citations(answer, [chunk()])

    assert report.total == 2
    assert report.valid == 2
    assert report.duplicate_labels == ["资料《zenframe.md》#0"]
    assert len(report.malformed_labels) == 2


def test_bare_chunk_reference_is_malformed_and_ignored_for_numeric_support() -> None:
    answer = "TLB 未命中会触发多次内存访问 [资料《zenframe.md》#0][#1]。"
    report = validate_citations(answer, [chunk()])

    assert report.malformed_labels == ["[#1]"]
    assert report.citations[0].numeric_conflict is False


def test_no_citations_returns_none_rate_not_zero() -> None:
    report = validate_citations("这是一个没有引用的回答。", [chunk()])

    assert report.total == 0
    assert report.illegal_rate is None
    assert report.has_citations is False


def test_numeric_mismatch_is_a_signal_not_a_citation_failure() -> None:
    source = chunk(
        chunk_id=2,
        document_id=11,
        name="auroragate.md",
        content="AuroraGate 的密钥轮换周期是 42 天。",
    )
    answer = "AuroraGate 的密钥轮换周期是 45 天 [资料《auroragate.md》#0]。"
    report = validate_citations(answer, [source])

    assert report.valid == 1
    assert report.citations[0].numeric_conflict is True
    assert report.citations[0].lexical_support == "numeric_conflict"


def test_unrelated_claim_gets_weak_or_no_lexical_support() -> None:
    answer = "今天的天气很好，适合散步 [资料《zenframe.md》#0]。"
    report = validate_citations(answer, [chunk()])

    assert report.valid == 1
    assert report.citations[0].lexical_support in {"none", "weak"}


def test_citation_after_sentence_period_uses_previous_sentence_as_claim() -> None:
    answer = "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。[资料《zenframe.md》#0]"
    report = validate_citations(answer, [chunk()])

    citation = report.citations[0]
    assert citation.valid is True
    assert citation.claim == "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒"
    assert citation.lexical_support == "strong"


def test_citation_on_source_line_uses_the_previous_claim_sentence() -> None:
    answer = (
        "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。\n\n"
        "> 来源：[资料《zenframe.md》#0]"
    )
    report = validate_citations(answer, [chunk()])

    citation = report.citations[0]
    assert citation.valid is True
    assert citation.claim == "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒"
    assert citation.lexical_support == "strong"


def test_parenthesized_or_dash_prefixed_source_line_falls_back_to_claim() -> None:
    for tail in ("（来源：[资料《zenframe.md》#0]）", "> —— [资料《zenframe.md》#0]"):
        answer = f"ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒。\n\n{tail}"
        report = validate_citations(answer, [chunk()])

        citation = report.citations[0]
        assert citation.claim == "ZenFrame 的提醒模块通过 MQTT 发送云端定时提醒"
        assert citation.lexical_support == "strong"


def test_chinese_numeral_and_digit_are_not_a_numeric_conflict() -> None:
    source = chunk(content="超过 30 秒的请求会被标记为 TIMEOUT 并重试一次。")
    answer = "超过 30 秒会标记为 TIMEOUT 并重试 1 次 [资料《zenframe.md》#0]。"
    report = validate_citations(answer, [source])

    assert report.citations[0].numeric_conflict is False
    assert report.citations[0].lexical_support == "strong"


def test_chinese_word_liangzhe_is_not_treated_as_number_two() -> None:
    source = chunk(content="MQTT 3.1.1 用于云端提醒。")
    answer = "两者协议不同，MQTT 3.1.1 只用于云端提醒 [资料《zenframe.md》#0]。"
    report = validate_citations(answer, [source])

    assert report.citations[0].numeric_conflict is False


def test_indefinite_yi_ge_is_not_a_factual_numeric_conflict() -> None:
    """「一个历史值」不是事实数字；它不能把正确归属的引用误报为冲突。"""
    source = chunk(
        content=(
            "2024 年及以前的旧调度器使用 12 秒的重试窗口，该版本已在 2025 年下线。"
        )
    )
    answer = (
        "这里容易混进来一个历史值：2024 年及以前的旧调度器用的是 12 秒，"
        "那个版本已经在 2025 年下线 [资料《zenframe.md》#0]。"
    )
    report = validate_citations(answer, [source])

    assert report.citations[0].numeric_conflict is False


def test_context_builder_keeps_heading_outside_the_citation_brackets() -> None:
    source = KnowledgeChunk(
        id=200,
        document_id=1,
        chunk_index=3,
        document_name="操作系统笔记.md",
        file_type="md",
        heading_path=["虚拟内存", "TLB"],
        content="TLB 是 MMU 内部的页表项缓存。",
    )
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message="TLB 是什么？",
    )
    state.knowledge = [source]
    state.trace = RunTrace(run_id=1)
    context = ContextBuilder().build(state)
    system = context.messages[0].content

    assert "- [资料《操作系统笔记.md》#3] ｜ 章节：虚拟内存 > TLB" in system
    assert "#3 ｜ 章节" not in system
    assert provided_labels_from_context(context, state.knowledge) == [
        "资料《操作系统笔记.md》#3"
    ]


def test_range_multi_index_and_decimal_labels_are_all_malformed() -> None:
    answer = (
        "范围 [资料《zenframe.md》#0–#2]，"
        "多序号 [资料《zenframe.md》#0, #1]，"
        "小数 [资料《zenframe.md》#0.5]，"
        "裸引用 [#1, #2]。"
    )
    report = validate_citations(answer, [chunk()])

    assert report.total == 0
    assert report.valid == 0
    assert len(report.malformed_labels) == 4
    assert "[资料《zenframe.md》#0–#2]" in report.malformed_labels
    assert "[资料《zenframe.md》#0, #1]" in report.malformed_labels
    assert "[资料《zenframe.md》#0.5]" in report.malformed_labels
    assert "[#1, #2]" in report.malformed_labels


def test_exact_label_next_to_a_malformed_one_is_still_counted_exactly() -> None:
    answer = (
        "事实 [资料《zenframe.md》#0]，"
        "另一个坏格式 [资料《zenframe.md》#0–#2]。"
    )
    report = validate_citations(answer, [chunk()])

    assert report.total == 1
    assert report.valid == 1
    assert report.malformed_labels == ["[资料《zenframe.md》#0–#2]"]
