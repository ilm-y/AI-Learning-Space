"""v2 评分规则的离线测试。

重点不是「让更多用例通过」，而是证明每条规则变化都有明确语义，并且 v1 的历史结果
不会被覆盖：同一个模型回答，v1 判错、v2 判对的差异必须能追溯到具体的同义/引用分类。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.citations import validate_citations
from app.schemas import KnowledgeChunk
from evaluation.rag import DATASET_PATH, DATASET_V2_PATH
from evaluation.rag.models import (
    AnswerObservation,
    ContextObservation,
    RetrievalObservation,
    RetrievedChunk,
    load_cases,
)
from evaluation.rag.scoring import score_case
from evaluation.rag.scoring_v2 import aggregate_v2, score_case_v2


def _cases(path: Path = DATASET_V2_PATH):
    return {case.id: case for case in load_cases(path)}


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
        content=content,
        score=0.9,
    )


def _retrieval(chunks: list[KnowledgeChunk]) -> RetrievalObservation:
    return RetrievalObservation(
        status="SUCCESS",
        chunks=[
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id or 0,
                document_name=chunk.document_name or "",
                chunk_index=chunk.chunk_index or 0,
                score=float(chunk.score or 0.0),
                content=chunk.content,
            )
            for chunk in chunks
        ],
        candidates=len(chunks),
        allowlist_size=len(chunks),
    )


def _context(chunks: list[KnowledgeChunk], mode: str = "rag") -> ContextObservation:
    return ContextObservation(
        mode=mode,
        provided_labels=[
            f"资料《{chunk.document_name}》#{chunk.chunk_index}" for chunk in chunks
        ],
        context_documents=[chunk.document_name or "" for chunk in chunks],
        context_chunk_ids=[chunk.id for chunk in chunks],
        sections=[
            {"name": f"knowledge:{chunk.id}", "dropped": False, "priority": 40}
            for chunk in chunks
        ],
    )


def _answer(text: str) -> AnswerObservation:
    return AnswerObservation(text=text, latency_ms=10, provider="mock", model="mock")


def _score(case, *, mode="rag", chunks, text, version="v2"):
    report = validate_citations(
        text,
        chunks,
        provided_labels=[f"资料《{c.document_name}》#{c.chunk_index}" for c in chunks],
    )
    fn = score_case_v2 if version == "v2" else score_case
    return fn(
        case,
        mode,
        retrieval=_retrieval(chunks),
        context=_context(chunks, mode=mode),
        answer=_answer(text),
        citation_report=report,
    )


def test_v1_dataset_is_preserved_and_v2_overrides_are_explicit():
    v1 = _cases(DATASET_PATH)
    v2 = _cases(DATASET_V2_PATH)

    assert len(v1) == len(v2) == 41
    assert v1["A12"].required_terms == ("多级页表", "内存访问")
    assert v1["A12"].required_bindings == (("多级页表", "内存访问"),)
    assert v2["A12"].required_bindings == ()
    assert v2["A12"].required_term_groups == (
        ("多级页表",),
        ("内存访问", "访存", "访问内存"),
    )
    assert v1["D04"].admission_patterns == ()
    assert v2["D04"].admission_patterns


def test_a12_synonym_answer_is_false_negative_in_v1_and_passes_v2():
    chunk = _chunk(
        chunk_id=10,
        document_name="operating-system.md",
        content="多级页表使一次 TLB 未命中需要多次访问内存。",
    )
    text = (
        "多级页表会逐级存放页表项。一次 TLB 未命中会造成多次内存访问。"
        "[资料《operating-system.md》#0]"
    )
    v1 = _score(_cases(DATASET_PATH)["A12"], chunks=[chunk], text=text, version="v1")
    v2 = _score(_cases()["A12"], chunks=[chunk], text=text, version="v2")

    assert v1.answer.passed is False
    assert v2.answer.passed is True
    assert v2.citation.passed is True


def test_d04_and_d05_require_admission_of_the_asked_subject_not_bare_negation():
    chunks_d04 = [
        _chunk(
            chunk_id=11,
            document_name="auroragate.md",
            content="AuroraGate 使用 RSA-4096 签名，密钥每 42 天轮换。",
        )
    ]
    text_d04 = "资料中没有一处提到 SDK，也没有说明它支持哪些编程语言。"
    v1 = _score(_cases(DATASET_PATH)["D04"], chunks=chunks_d04, text=text_d04, version="v1")
    v2 = _score(_cases()["D04"], chunks=chunks_d04, text=text_d04, version="v2")
    assert v1.answer.passed is False
    assert v2.answer.passed is True

    chunks_d05 = [
        _chunk(
            chunk_id=12,
            document_name="helios-compute.md",
            content="HeliosCompute 的调度器分为准入控制、打分与抢占三个阶段。",
        )
    ]
    text_d05 = "资料未涉及商业授权和定价，也没有提及许可证价格。"
    v1 = _score(_cases(DATASET_PATH)["D05"], chunks=chunks_d05, text=text_d05, version="v1")
    v2 = _score(_cases()["D05"], chunks=chunks_d05, text=text_d05, version="v2")
    assert v1.answer.passed is False
    assert v2.answer.passed is True


def test_contradictory_no_evidence_answer_is_not_rescued_by_a_negation_word():
    chunks = [
        _chunk(
            chunk_id=13,
            document_name="auroragate.md",
            content="AuroraGate 使用 RSA-4096 签名，密钥每 42 天轮换。",
        )
    ]
    text = "虽然没有提到 SDK，但官方 SDK 当前仅支持 Java 和 Python [资料《auroragate.md》#0]。"
    score = _score(_cases()["D04"], chunks=chunks, text=text, version="v2")

    assert score.answer.passed is False

    # 真实 4.3 失败样本：前半句承认「资料没有提及」，后半句却把否定升级成
    # 「官方未提供任何语言的 SDK」。这不是同义表达，而是无证据的绝对化断言。
    overclaim = "资料中没有提及 SDK，但 AuroraGate 官方未提供任何语言的 SDK。"
    score = _score(_cases()["D04"], chunks=chunks, text=overclaim, version="v2")
    assert score.answer.passed is False
    assert score.answer.detail["forbiddenHits"]


def test_e05_legal_contrast_citation_is_not_a_disallowed_source_in_v2():
    zen = _chunk(chunk_id=14, content="ZenFrame 的提醒模块通过 MQTT 3.1.1 发送云端定时提醒。")
    zeal = _chunk(
        chunk_id=15,
        document_name="zealdeck.md",
        content="ZealDeck 的提醒模块通过 Kafka 发送云端定时提醒。",
    )
    text = (
        "ZenFrame 的提醒协议不是 Kafka，而是 MQTT 3.1.1 [资料《zenframe.md》#0]；"
        "Kafka 是 ZealDeck 的协议 [资料《zealdeck.md》#0]。"
    )
    v1 = _score(
        _cases(DATASET_PATH)["E05"], mode="rag+state", chunks=[zen, zeal], text=text, version="v1"
    )
    v2 = _score(
        _cases()["E05"], mode="rag+state", chunks=[zen, zeal], text=text, version="v2"
    )

    assert v1.citation.passed is False
    assert v2.citation.passed is True
    assert v2.answer.passed is True
    assert v2.citation.detail["extraValidDocuments"] == []


def test_d02_adjacent_supported_citations_do_not_fail_v2():
    """D02 问 CEO，资料没有答案；但回答可以引用同一文档里的真实技术事实。

    4.2 的 v1 规则把「问题本身无证据」误写成了「整条回答禁止任何引用」，
    真实模型引用 MQTT/星闪等有据事实时会被判错。v2 只禁止伪造/非法引用，
    不禁止对本轮真实片段的有据引用。
    """
    chunk = _chunk(chunk_id=30, content="ZenFrame 的提醒模块通过 MQTT 3.1.1 发送云端定时提醒。")
    text = (
        "资料里没有提到 ZenFrame 的 CEO 是谁。"
        "已知事实只有提醒模块使用 MQTT 3.1.1 [资料《zenframe.md》#0]。"
    )
    score = _score(_cases()["D02"], chunks=[chunk], text=text, version="v2")

    assert score.answer.passed is True
    assert score.citation.passed is True
    assert score.auto_pass is True

    # 同义表达「没有提及任何……CEO 信息」也必须识别，不能只认「没有提到」。
    synonym = (
        "文档中没有提及任何关于公司架构、创始人或 CEO 的信息。"
        "已知事实只有提醒模块使用 MQTT 3.1.1 [资料《zenframe.md》#0]。"
    )
    score = _score(_cases()["D02"], chunks=[chunk], text=synonym, version="v2")
    assert score.answer.passed is True
    assert score.citation.passed is True


def test_extra_valid_source_is_counted_but_does_not_fail_v2():
    zen = _chunk(chunk_id=16)
    zeal = _chunk(
        chunk_id=17,
        document_name="zealdeck.md",
        content="ZealDeck 的提醒模块通过 Kafka 发送云端定时提醒。",
    )
    text = (
        "ZenFrame 使用 MQTT [资料《zenframe.md》#0]，"
        "对比之下 ZealDeck 使用 Kafka [资料《zealdeck.md》#0]。"
    )
    score = _score(_cases()["A01"], chunks=[zen, zeal], text=text, version="v2")

    assert score.citation.passed is True
    assert score.citation.detail["extraValidDocuments"] == ["zealdeck.md"]
    assert score.citation.detail["metrics"]["extraValidDocuments"] == 1


def test_forbidden_and_missing_required_sources_still_fail_v2():
    aurora = _chunk(
        chunk_id=18,
        document_name="auroragate.md",
        content="AuroraGate 的密钥轮换周期是 42 天。",
    )
    zen = _chunk(chunk_id=19)
    forbidden_text = (
        "AuroraGate 是 42 天 [资料《auroragate.md》#0]，"
        "ZenFrame 也是 42 天 [资料《zenframe.md》#0]。"
    )
    forbidden_score = _score(
        _cases()["D06"], chunks=[aurora, zen], text=forbidden_text, version="v2"
    )
    assert forbidden_score.citation.passed is False
    assert forbidden_score.citation.detail["forbiddenDocumentHits"] == ["zenframe.md"]

    missing_text = "ZenFrame 使用 MQTT [资料《zealdeck.md》#0]。"
    zeal = _chunk(
        chunk_id=20,
        document_name="zealdeck.md",
        content="ZealDeck 使用 Kafka。",
    )
    missing_score = _score(
        _cases()["A01"], chunks=[zen, zeal], text=missing_text, version="v2"
    )
    assert missing_score.citation.passed is False
    assert missing_score.citation.detail["missingRequiredDocuments"] == ["zenframe.md"]


def test_malformed_labels_still_fail_v2():
    chunk = _chunk(chunk_id=21)
    text = "ZenFrame 使用 MQTT [资料《zenframe.md》#0–#1]。"
    score = _score(_cases()["A01"], chunks=[chunk], text=text, version="v2")

    assert score.citation.passed is False
    assert score.citation.detail["malformedLabels"] == ["[资料《zenframe.md》#0–#1]"]


def test_aggregate_v2_reports_each_metric_with_its_own_denominator():
    zen = _chunk(chunk_id=22)
    zeal = _chunk(chunk_id=23, document_name="zealdeck.md", content="ZealDeck 使用 Kafka。")
    aurora = _chunk(
        chunk_id=24,
        document_name="auroragate.md",
        content="AuroraGate 的密钥轮换周期是 42 天。",
    )
    missing = _score(
        _cases()["A01"],
        chunks=[zen, zeal],
        text="ZenFrame 使用 MQTT [资料《zealdeck.md》#0]。",
        version="v2",
    )
    forbidden = _score(
        _cases()["D06"],
        chunks=[aurora, zen],
        text=(
            "AuroraGate 是 42 天 [资料《auroragate.md》#0]，"
            "ZenFrame 也是 42 天 [资料《zenframe.md》#0]。"
        ),
        version="v2",
    )
    summary = aggregate_v2([missing, forbidden])
    data = summary["modes"]["rag"]

    assert data["metrics"]["required_source_missing_rate"]["numerator"] == 1
    assert data["metrics"]["required_source_missing_rate"]["denominator"] == 1
    assert data["metrics"]["forbidden_source_rate"]["numerator"] == 1
    assert data["metrics"]["forbidden_source_rate"]["denominator"] == 1
    assert data["metrics"]["extra_valid_source_rate"]["numerator"] == 1
    assert data["metrics"]["extra_valid_source_rate"]["denominator"] == 2
    assert data["metrics"]["illegal_label_rate"]["denominator"] == 3
    assert data["metrics"]["malformed_label_rate"]["denominator"] == 3
    assert data["metrics"]["citation_support_warning_rate"]["denominator"] == 3
    assert all(
        {"numerator", "denominator", "rate", "definition"} <= set(metric)
        for metric in data["metrics"].values()
    )
