"""Phase 4.1：事实级来源归属的确定性回归。

这里只断言**代码能确定判断**的部分：
- 标注是否指向本轮实际注入的 chunk；
- 标注句里的数字是否能被该 chunk 的原文支持（`numeric_conflict` 诊断信号）；
- Prompt 是否明确要求不同来源/版本的事实拆句并分别标注。

`numeric_conflict=false` 不等于「事实为真」，`true` 也不自动等于「回答错误」。
真正的语义蕴含仍需人工复核，见 `phase4.1-agent-human-review.md`。
"""

from __future__ import annotations

from app.citations import validate_citations
from app.prompts import BASE_SYSTEM_INSTRUCTION
from app.schemas import KnowledgeChunk

CURRENT = KnowledgeChunk(
    id=18,
    document_id=20,
    chunk_index=0,
    document_name="orbit_current.md",
    content=(
        "ORBIT-4721 当前调度器使用 17 秒的重试窗口，从 2025 版起生效。"
        "不要改回旧值；如果实际观测值不符合预期，应先核对部署版本。"
    ),
)

LEGACY = KnowledgeChunk(
    id=19,
    document_id=21,
    chunk_index=0,
    document_name="orbit_legacy.md",
    content=(
        "ORBIT-4721 旧调度器（2024 年及以前）使用 12 秒的重试窗口，"
        "该版本已在 2025 年下线，仅用于迁移考古。"
    ),
)


def test_prompt_requires_splitting_facts_from_different_sources() -> None:
    assert "拆成独立句子" in BASE_SYSTEM_INSTRUCTION
    assert "分别标注它自己的来源" in BASE_SYSTEM_INSTRUCTION
    assert "只在句末标注其中一个来源" in BASE_SYSTEM_INSTRUCTION
    assert "静默混合" in BASE_SYSTEM_INSTRUCTION


def test_single_source_supports_a_single_fact() -> None:
    answer = "当前调度器使用 17 秒的重试窗口 [资料《orbit_current.md》#0]。"
    report = validate_citations(answer, [CURRENT, LEGACY])

    assert report.total == 1
    assert report.valid == 1
    assert report.invalid == 0
    citation = report.citations[0]
    assert citation.chunk_id == 18
    assert citation.document_id == 20
    assert citation.as_dict()["documentId"] == 20
    assert citation.numeric_conflict is False
    assert citation.lexical_support in {"strong", "weak"}


def test_two_sources_each_support_their_own_fact_when_split_into_sentences() -> None:
    answer = (
        "当前调度器使用 17 秒的重试窗口 [资料《orbit_current.md》#0]。"
        "旧调度器使用 12 秒的重试窗口 [资料《orbit_legacy.md》#0]。"
    )
    report = validate_citations(answer, [CURRENT, LEGACY])

    assert report.total == 2
    assert report.valid == 2
    assert report.invalid == 0
    assert report.numeric_conflict_count == 0
    assert [citation.chunk_id for citation in report.citations] == [18, 19]
    assert [citation.document_id for citation in report.citations] == [20, 21]


def test_merged_versions_in_one_sentence_are_flagged_as_numeric_conflict() -> None:
    """Phase 4 的 `insufficient_detail` 形态：两个版本合并，只标现行规范。"""
    answer = "当前调度器是 17 秒，旧调度器的 12 秒不能当作现行值 [资料《orbit_current.md》#0]。"
    report = validate_citations(answer, [CURRENT, LEGACY])

    assert report.valid == 1
    assert report.invalid == 0
    assert report.numeric_conflict_count == 1
    citation = report.citations[0]
    assert citation.chunk_id == 18
    assert citation.numeric_conflict is True
    assert citation.lexical_support == "numeric_conflict"


def test_phase4_legacy_only_sentence_keeps_its_numeric_conflict_regression() -> None:
    """Phase 4 的 `legacy_only` 形态：引用现行规范，却写出旧版的 12 秒。"""
    answer = (
        "如果发现 17 秒不符合预期，应先核对部署版本，"
        "而不是改回 12 秒 [资料《orbit_current.md》#0]。"
    )
    report = validate_citations(answer, [CURRENT, LEGACY])

    assert report.valid == 1
    assert report.citations[0].chunk_id == 18
    assert report.citations[0].numeric_conflict is True


def test_format_valid_but_body_fact_not_supported_is_reported_as_unsupported() -> None:
    answer = "ORBIT-4721 的仪表盘默认颜色是蓝色 [资料《orbit_current.md》#0]。"
    report = validate_citations(answer, [CURRENT, LEGACY])

    # 标签和 chunk 都存在，所以不是非法引用；但来源没有支持这个事实。
    assert report.valid == 1
    assert report.invalid == 0
    assert report.citations[0].lexical_support in {"none", "weak"}
    assert report.unsupported_count == 1


def test_nonexistent_chunk_index_is_illegal_even_if_the_document_exists() -> None:
    answer = "当前调度器使用 17 秒 [资料《orbit_current.md》#9]。"
    report = validate_citations(answer, [CURRENT, LEGACY])

    assert report.total == 1
    assert report.valid == 0
    assert report.invalid == 1
    assert report.invalid_labels == ["[资料《orbit_current.md》#9]"]
    assert report.citations[0].document_id is None
    assert report.numeric_conflict_count == 0


def test_correct_fact_pointing_to_wrong_source_is_a_signal_not_a_legal_error() -> None:
    """事实本身正确，但标注指向另一个来源：格式合法，数字归属冲突。"""
    wrong = "旧调度器使用 12 秒的重试窗口 [资料《orbit_current.md》#0]。"
    right = "旧调度器使用 12 秒的重试窗口 [资料《orbit_legacy.md》#0]。"

    wrong_report = validate_citations(wrong, [CURRENT, LEGACY])
    right_report = validate_citations(right, [CURRENT, LEGACY])

    assert wrong_report.valid == 1
    assert wrong_report.citations[0].chunk_id == 18
    assert wrong_report.citations[0].document_id == 20
    assert wrong_report.citations[0].numeric_conflict is True

    assert right_report.valid == 1
    assert right_report.citations[0].chunk_id == 19
    assert right_report.citations[0].document_id == 21
    assert right_report.citations[0].numeric_conflict is False
