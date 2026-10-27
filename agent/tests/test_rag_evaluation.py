"""RAG 评测集与四层评分的离线测试。

这些测试不访问网络、不调用真实模型：它们只保证评测数据没有写坏、
评分规则能把「检索到 / 进了上下文 / 答对 / 引用合法」分开。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.citations import validate_citations
from app.schemas import KnowledgeChunk
from evaluation.rag import DATASET_PATH
from evaluation.rag.models import (
    AnswerObservation,
    ContextObservation,
    RetrievalObservation,
    RetrievedChunk,
    load_cases,
)
from evaluation.rag.scoring import aggregate, score_case


def _cases():
    return {case.id: case for case in load_cases(DATASET_PATH)}


def _chunk(
    *,
    chunk_id: int = 1,
    document_name: str = "zenframe.md",
    chunk_index: int = 0,
    content: str = "ZenFrame 的提醒模块通过 MQTT 3.1.1 发送云端定时提醒。",
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id,
        document_id=1,
        chunk_index=chunk_index,
        document_name=document_name,
        file_type="md",
        heading_path=["提醒"],
        content=content,
        score=0.91,
    )


def _retrieval(chunks: list[KnowledgeChunk], status: str = "SUCCESS") -> RetrievalObservation:
    return RetrievalObservation(
        status=status,
        chunks=[
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id or 0,
                document_name=chunk.document_name or "",
                chunk_index=chunk.chunk_index or 0,
                score=float(chunk.score or 0.0),
                content=chunk.content,
                heading_path=tuple(chunk.heading_path),
            )
            for chunk in chunks
        ],
        candidates=len(chunks),
        allowlist_size=1,
    )


def _context(
    chunks: list[KnowledgeChunk],
    *,
    mode: str = "rag",
    labels: list[str] | None = None,
    detected: list[str] | None = None,
) -> ContextObservation:
    if labels is None:
        labels = [f"资料《{chunk.document_name}》#{chunk.chunk_index}" for chunk in chunks]
    return ContextObservation(
        mode=mode,
        provided_labels=labels,
        context_documents=[chunk.document_name or "" for chunk in chunks],
        context_chunk_ids=[chunk.id for chunk in chunks],
        detected_concepts=detected or [],
        sections=[
            {"name": "knowledge:%d" % chunk.id, "dropped": False, "priority": 40}
            for chunk in chunks
        ],
    )


def _answer(text: str) -> AnswerObservation:
    return AnswerObservation(text=text, latency_ms=100, provider="openai", model="qwen-plus")


def test_dataset_is_fixed_and_covers_all_categories():
    cases = load_cases(DATASET_PATH)
    assert len(cases) == 41
    counts = {category: 0 for category in "ABCDEF"}
    for case in cases:
        counts[case.category] += 1
    assert counts == {"A": 12, "B": 8, "C": 6, "D": 6, "E": 5, "F": 4}
    assert len({case.id for case in cases}) == len(cases)


def test_dataset_scopes_manual_and_degradation_cases():
    cases = _cases()
    assert cases["E05"].modes == ("rag+state",)
    assert cases["E05"].manual_review is True
    for case_id in ("F01", "F03", "F04"):
        assert cases[case_id].modes == ("rag", "rag+state")
    assert cases["F02"].modes == ("plain", "rag", "rag+state")


def test_perfect_a01_run_passes_all_four_layers():
    case = _cases()["A01"]
    chunk = _chunk()
    answer = "ZenFrame 使用 MQTT 发送云端定时提醒。[资料《zenframe.md》#0]"
    report = validate_citations(
        answer, [chunk], provided_labels=["资料《zenframe.md》#0"]
    )
    score = score_case(
        case,
        "rag",
        retrieval=_retrieval([chunk]),
        context=_context([chunk]),
        answer=_answer(answer),
        citation_report=report,
    )
    assert score.retrieval.passed is True
    assert score.context.passed is True
    assert score.answer.passed is True
    assert score.citation.passed is True
    assert score.auto_pass is True
    assert report.citations[0].lexical_support == "strong"


def test_illegal_citation_label_fails_even_when_answer_facts_are_right():
    case = _cases()["A01"]
    chunk = _chunk()
    answer = "ZenFrame 使用 MQTT 发送云端定时提醒。[资料《zenframe.md》#99]"
    report = validate_citations(
        answer, [chunk], provided_labels=["资料《zenframe.md》#0"]
    )
    score = score_case(
        case,
        "rag",
        retrieval=_retrieval([chunk]),
        context=_context([chunk]),
        answer=_answer(answer),
        citation_report=report,
    )
    assert score.answer.passed is True
    assert score.citation.passed is False
    assert score.auto_pass is False
    assert report.invalid_labels == ["[资料《zenframe.md》#99]"]


def test_legal_label_with_unrelated_source_is_reported_as_unsupported():
    case = _cases()["A01"]
    chunk = _chunk(
        content="NovaCache 生产集群 B 的默认请求超时是 60 秒，不适用于其他集群。"
    )
    answer = "ZenFrame 使用 MQTT 发送云端定时提醒。[资料《zenframe.md》#0]"
    report = validate_citations(
        answer, [chunk], provided_labels=["资料《zenframe.md》#0"]
    )
    score = score_case(
        case,
        "rag",
        retrieval=_retrieval([chunk]),
        context=_context([chunk]),
        answer=_answer(answer),
        citation_report=report,
    )
    # 标签合法只说明“这个片段确实在上下文里”，不说明它支持这句话。
    assert score.citation.passed is True
    detail = score.citation.detail
    assert detail["supportCounts"]["none"] >= 1
    assert detail["unsupportedClaims"]
    assert score.auto_pass is True


def test_no_evidence_question_passes_without_fabricated_citation():
    case = _cases()["D01"]
    score = score_case(
        case,
        "rag",
        retrieval=RetrievalObservation(status="NO_MATCH"),
        context=ContextObservation(mode="rag"),
        answer=_answer("上传的资料里没有直接答案，我不能用一般知识冒充文档内容。"),
        citation_report=validate_citations("", []),
    )
    assert score.retrieval.passed is True
    assert score.answer.passed is True
    assert score.citation.passed is True
    assert score.auto_pass is True


def test_no_evidence_question_fails_if_it_fabricates_a_citation():
    case = _cases()["D01"]
    answer = "上传的资料里没有直接答案。[资料《zenframe.md》#0]"
    score = score_case(
        case,
        "rag",
        retrieval=RetrievalObservation(status="NO_MATCH"),
        context=ContextObservation(mode="rag"),
        answer=_answer(answer),
        citation_report=validate_citations(answer, [], provided_labels=[]),
    )
    assert score.answer.passed is True
    assert score.citation.passed is False
    assert score.auto_pass is False


def test_retrieval_degraded_still_requires_no_fake_citation():
    case = _cases()["F03"]
    score = score_case(
        case,
        "rag",
        retrieval=RetrievalObservation(status="DEGRADED", warning="Qdrant 不可用"),
        context=ContextObservation(mode="rag", warnings=["Qdrant 不可用"]),
        answer=_answer("知识检索降级，本次无法使用上传资料。"),
        citation_report=validate_citations("", []),
    )
    assert score.retrieval.passed is True
    assert score.context.passed is True
    assert score.answer.passed is True
    assert score.citation.passed is True
    assert score.auto_pass is True


def test_plain_mode_does_not_require_document_admission_for_no_evidence_question():
    case = _cases()["D01"]
    score = score_case(
        case,
        "plain",
        retrieval=RetrievalObservation(status="NOT_RETRIEVED"),
        context=ContextObservation(mode="plain"),
        answer=_answer("量子色动力学中的渐进自由是指……"),
        citation_report=validate_citations("", []),
    )
    assert score.retrieval.applicable is False
    assert score.answer.applicable is False
    assert score.citation.passed is True
    assert score.auto_pass is True


def test_dropped_knowledge_is_not_treated_as_context():
    case = _cases()["A01"]
    chunk = _chunk()
    score = score_case(
        case,
        "rag",
        retrieval=_retrieval([chunk]),
        context=ContextObservation(
            mode="rag",
            provided_labels=[],
            context_documents=[],
            dropped_knowledge=1,
            sections=[{"name": "knowledge:1", "dropped": True, "priority": 40}],
        ),
        answer=_answer("ZenFrame 使用 MQTT 发送云端定时提醒。"),
        citation_report=validate_citations("", [chunk], provided_labels=[]),
    )
    assert score.retrieval.passed is True
    assert score.context.passed is False
    assert score.citation.passed is False
    assert score.auto_pass is False


def test_aggregate_separates_answer_and_citation_rates():
    case = _cases()["A01"]
    chunk = _chunk()
    answer = "ZenFrame 使用 MQTT 发送云端定时提醒。[资料《zenframe.md》#0]"
    report = validate_citations(answer, [chunk], provided_labels=["资料《zenframe.md》#0"])
    good = score_case(
        case,
        "rag",
        retrieval=_retrieval([chunk]),
        context=_context([chunk]),
        answer=_answer(answer),
        citation_report=report,
    )
    summary = aggregate([good])
    assert summary["modes"]["rag"]["answerPassRate"] == 1.0
    assert summary["modes"]["rag"]["citationPassRate"] == 1.0
    assert summary["modes"]["rag"]["illegalCitationRate"] == 0.0
