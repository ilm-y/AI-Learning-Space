"""四层评分：检索、上下文、答案事实、引用。

这四层必须分开看：

1. **retrieval** —— 该被找到的文档/片段是否被检索到；
2. **context** —— 检索结果是否真的进入了模型实际看到的上下文（Token 预算可能丢掉）；
3. **answer** —— 回答是否包含标准答案要求的事实（词面规则，不冒充语义判分）；
4. **citation** —— 引用标注是否合法、是否覆盖必需来源、来源是否支持该句
   （“支持度”只是词面启发式，最终仍需人工抽样复核）。

`answer` 与 `citation` 不能互相替代：答案里事实对了但引用编造，citation 失败；
引用标签都存在但来源与说法无关，citation 的 support 信号会降级。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.citations import CitationReport

from .models import AnswerObservation, ContextObservation, EvalCase, RetrievalObservation

_SENTENCE_SPLIT = re.compile(r"[。！？!?\n；;]")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Check:
    name: str
    passed: bool | None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class LayerScore:
    """一层的评分。`passed=None` 表示该层在当前模式下不适用。"""

    applicable: bool
    passed: bool | None
    checks: list[Check] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
            "detail": self.detail,
        }


@dataclass
class CaseScore:
    case_id: str
    category: str
    mode: str
    retrieval: LayerScore
    context: LayerScore
    answer: LayerScore
    citation: LayerScore
    auto_pass: bool
    manual_review: bool
    latency_ms: int
    error: str | None
    notes: str = ""

    @property
    def status(self) -> str:
        if self.manual_review:
            return "manual"
        return "pass" if self.auto_pass else "fail"

    @property
    def checks(self) -> list[Check]:
        result: list[Check] = []
        for layer in (self.retrieval, self.context, self.answer, self.citation):
            result.extend(layer.checks)
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "category": self.category,
            "mode": self.mode,
            "status": self.status,
            "autoPass": self.auto_pass,
            "manualReview": self.manual_review,
            "latencyMs": self.latency_ms,
            "error": self.error,
            "notes": self.notes,
            "layers": {
                "retrieval": self.retrieval.as_dict(),
                "context": self.context.as_dict(),
                "answer": self.answer.as_dict(),
                "citation": self.citation.as_dict(),
            },
        }


def score_case(
    case: EvalCase,
    mode: str,
    *,
    retrieval: RetrievalObservation,
    context: ContextObservation,
    answer: AnswerObservation,
    citation_report: CitationReport,
) -> CaseScore:
    retrieval_score = _score_retrieval(case, mode, retrieval)
    context_score = _score_context(case, mode, retrieval, context)
    answer_score = _score_answer(case, mode, answer)
    citation_score = _score_citation(case, mode, citation_report)

    applicable = [
        layer
        for layer in (retrieval_score, context_score, answer_score, citation_score)
        if layer.applicable and layer.passed is not None
    ]
    auto_pass = bool(applicable) and all(bool(layer.passed) for layer in applicable)
    return CaseScore(
        case_id=case.id,
        category=case.category,
        mode=mode,
        retrieval=retrieval_score,
        context=context_score,
        answer=answer_score,
        citation=citation_score,
        auto_pass=auto_pass,
        manual_review=case.manual_review,
        latency_ms=answer.latency_ms,
        error=answer.error,
        notes=case.notes,
    )


# ===================== 1. 检索 =====================


def _score_retrieval(
    case: EvalCase, mode: str, retrieval: RetrievalObservation
) -> LayerScore:
    if mode == "plain":
        return LayerScore(
            applicable=False,
            passed=None,
            checks=[Check("plain_mode_no_retrieval", None, "plain 模式不检索文档")],
            detail={"status": "NOT_RETRIEVED"},
        )

    expected = set(case.expected_documents)
    present = set(retrieval.document_names)
    missing = sorted(expected - present)
    status_ok = retrieval.status in case.expected_status
    non_success_empty = retrieval.status == "SUCCESS" or not retrieval.chunks
    checks = [
        Check(
            "status_expected",
            status_ok,
            f"expected={list(case.expected_status)} actual={retrieval.status}",
        ),
        Check("expected_documents_present", not missing, f"missing={missing}"),
        Check(
            "non_success_has_no_chunks",
            non_success_empty,
            f"status={retrieval.status} chunks={len(retrieval.chunks)}",
        ),
    ]
    passed = status_ok and not missing and non_success_empty
    return LayerScore(
        applicable=True,
        passed=passed,
        checks=checks,
        detail={
            "status": retrieval.status,
            "warning": retrieval.warning,
            "candidates": retrieval.candidates,
            "documents": retrieval.document_names,
            "missingDocuments": missing,
            "chunkCount": len(retrieval.chunks),
            "durationMs": retrieval.duration_ms,
            "collection": retrieval.collection,
            "allowlistSize": retrieval.allowlist_size,
            "error": retrieval.error,
        },
    )


# ===================== 2. 上下文 =====================


def _score_context(
    case: EvalCase,
    mode: str,
    retrieval: RetrievalObservation,
    context: ContextObservation,
) -> LayerScore:
    state_sections = [
        section
        for section in context.sections
        if str(section.get("name", "")).split(":", 1)[0] in {"understanding", "memory"}
        and not section.get("dropped")
    ]
    if mode == "plain":
        passed = not context.provided_labels and not context.context_documents
        return LayerScore(
            applicable=True,
            passed=passed,
            checks=[
                Check(
                    "plain_mode_has_no_knowledge",
                    passed,
                    f"labels={context.provided_labels}",
                )
            ],
            detail={
                "providedLabels": context.provided_labels,
                "contextDocuments": context.context_documents,
                "stateSections": len(state_sections),
                "estimatedTokens": context.estimated_tokens,
            },
        )

    expected = set(case.expected_documents)
    present = set(context.context_documents)
    missing = sorted(expected - present)
    non_success_no_knowledge = (
        retrieval.status == "SUCCESS" or not context.provided_labels
    )
    # 问题没有命中任何受控 Concept 时，rag+state 也没有可渲染的学习状态；
    # 这不是上下文组装失败，不能因此判负。
    state_ok = mode != "rag+state" or not context.detected_concepts or bool(state_sections)
    checks = [
        Check("expected_documents_in_context", not missing, f"missing={missing}"),
        Check(
            "non_success_context_has_no_knowledge",
            non_success_no_knowledge,
            f"status={retrieval.status} labels={context.provided_labels}",
        ),
        Check(
            "learning_state_in_context",
            state_ok,
            f"mode={mode} stateSections={len(state_sections)}",
        ),
    ]
    passed = not missing and non_success_no_knowledge and state_ok
    return LayerScore(
        applicable=True,
        passed=passed,
        checks=checks,
        detail={
            "providedLabels": context.provided_labels,
            "contextDocuments": context.context_documents,
            "contextChunkIds": context.context_chunk_ids,
            "missingDocuments": missing,
            "droppedKnowledge": context.dropped_knowledge,
            "detectedConcepts": context.detected_concepts,
            "stateSections": len(state_sections),
            "estimatedTokens": context.estimated_tokens,
            "warnings": context.warnings,
        },
    )


# ===================== 3. 答案事实 =====================


def _score_answer(case: EvalCase, mode: str, answer: AnswerObservation) -> LayerScore:
    # plain 模式没有文档；对“无证据问题”要求“资料里没有”是不公平的。
    # 这一层标记为不适用，但回答文本仍然保留，供人工对比三种模式。
    if mode == "plain" and case.category in {"D", "F"}:
        return LayerScore(
            applicable=False,
            passed=None,
            checks=[
                Check(
                    "plain_mode_no_docs_admission_na",
                    None,
                    "plain 模式不接收文档，不要求“资料里没有”式回答",
                )
            ],
            detail={"admissionApplicable": False},
        )

    text = answer.text or ""
    norm = _normalize(text)
    if answer.error:
        return LayerScore(
            applicable=True,
            passed=False,
            checks=[Check("answer_generated", False, answer.error)],
            detail={"error": answer.error},
        )

    missing_terms = [term for term in case.required_terms if _normalize(term) not in norm]
    missing_bindings = [
        list(pair)
        for pair in case.required_bindings
        if not _binding_present(text, pair[0], pair[1])
    ]
    missing_numbers = [number for number in case.required_numbers if not _number_present(text, number)]
    phrases_found = [
        phrase for phrase in case.required_phrases_any if _normalize(phrase) in norm
    ]
    phrases_ok = not case.required_phrases_any or bool(phrases_found)
    forbidden_hits = [
        pattern for pattern in case.forbidden_patterns if re.search(pattern, text)
    ]
    admission_found = [
        term for term in case.admission_terms_any if _normalize(term) in norm
    ]
    admission_ok = not case.admission_terms_any or bool(admission_found)

    checks = [
        Check("required_terms", not missing_terms, f"missing={missing_terms}"),
        Check("required_bindings", not missing_bindings, f"missing={missing_bindings}"),
        Check("required_numbers", not missing_numbers, f"missing={missing_numbers}"),
        Check(
            "required_phrases_any",
            phrases_ok,
            f"expectedAny={list(case.required_phrases_any)} found={phrases_found}",
        ),
        Check("forbidden_patterns", not forbidden_hits, f"hits={forbidden_hits}"),
        Check(
            "admission_terms_any",
            admission_ok,
            f"expectedAny={list(case.admission_terms_any)} found={admission_found}",
        ),
    ]
    passed = not missing_terms and not missing_bindings and not missing_numbers and phrases_ok and not forbidden_hits and admission_ok
    return LayerScore(
        applicable=True,
        passed=passed,
        checks=checks,
        detail={
            "missingTerms": missing_terms,
            "missingBindings": missing_bindings,
            "missingNumbers": missing_numbers,
            "phrasesFound": phrases_found,
            "forbiddenHits": forbidden_hits,
            "admissionFound": admission_found,
            "answerChars": len(text),
        },
    )


# ===================== 4. 引用 =====================


def _score_citation(
    case: EvalCase, mode: str, report: CitationReport
) -> LayerScore:
    all_cited_docs = sorted({citation.document_name for citation in report.citations})
    valid_docs = set(report.cited_documents)
    required = set(case.required_citation_documents)
    allowed = set(case.allowed_citation_documents)
    forbidden = set(case.forbidden_citation_documents)

    missing_required = sorted(required - valid_docs)
    forbidden_hit = sorted(forbidden & set(all_cited_docs))
    disallowed_hit = sorted(set(all_cited_docs) - allowed) if allowed else []

    if mode == "plain":
        expectation = "forbidden_no_context"
        passed = report.total == 0 and not report.malformed_labels
    elif case.citation_expectation == "required":
        expectation = "required"
        passed = (
            report.total > 0
            and report.invalid == 0
            and not report.malformed_labels
            and not missing_required
            and not forbidden_hit
            and not disallowed_hit
        )
    elif case.citation_expectation == "forbidden":
        expectation = "forbidden"
        passed = report.total == 0 and not report.malformed_labels
    else:
        expectation = "optional"
        passed = (
            report.invalid == 0
            and not report.malformed_labels
            and not forbidden_hit
            and not disallowed_hit
        )

    support_counts = {"strong": 0, "weak": 0, "none": 0, "numeric_conflict": 0}
    unsupported: list[dict[str, Any]] = []
    for citation in report.citations:
        if not citation.valid:
            continue
        support_counts[citation.lexical_support] = support_counts.get(citation.lexical_support, 0) + 1
        if citation.lexical_support in {"none", "numeric_conflict"}:
            unsupported.append(
                {
                    "label": citation.raw,
                    "claim": citation.claim,
                    "support": citation.lexical_support,
                    "numericConflict": citation.numeric_conflict,
                }
            )

    checks = [
        Check(
            "citation_expectation",
            passed,
            f"expectation={expectation} total={report.total}",
        ),
        Check("no_illegal_labels", report.invalid == 0, f"invalid={report.invalid_labels}"),
        Check(
            "no_malformed_labels",
            not report.malformed_labels,
            f"malformed={report.malformed_labels}",
        ),
        Check(
            "required_documents_cited",
            not missing_required,
            f"missing={missing_required}",
        ),
        Check(
            "forbidden_documents_not_cited",
            not forbidden_hit,
            f"hits={forbidden_hit}",
        ),
        Check(
            "cited_documents_allowed",
            not disallowed_hit,
            f"disallowed={disallowed_hit}",
        ),
    ]
    return LayerScore(
        applicable=True,
        passed=passed,
        checks=checks,
        detail={
            "expectation": expectation,
            "total": report.total,
            "valid": report.valid,
            "invalid": report.invalid,
            "illegalRate": report.illegal_rate,
            "invalidLabels": report.invalid_labels,
            "malformedLabels": report.malformed_labels,
            "duplicateLabels": report.duplicate_labels,
            "providedLabels": report.provided_labels,
            "citedDocuments": all_cited_docs,
            "validCitedDocuments": sorted(valid_docs),
            "missingRequiredDocuments": missing_required,
            "forbiddenDocumentHits": forbidden_hit,
            "disallowedDocumentHits": disallowed_hit,
            "supportCounts": support_counts,
            "unsupportedClaims": unsupported,
            "citations": [citation.as_dict() for citation in report.citations],
        },
    )


# ===================== 文本工具 =====================


def _normalize(text: str) -> str:
    """去掉空白再做子串匹配，避免「集群 A / 集群A」「RSA-4096 / RSA 4096」漏判。

    注意：这只用于**评分规则**，不用于引用合法性；引用标签必须精确匹配。
    """
    return _WHITESPACE.sub("", text or "").casefold()


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


def _binding_present(text: str, first: str, second: str) -> bool:
    a = _normalize(first)
    b = _normalize(second)
    return any(a in _normalize(sentence) and b in _normalize(sentence) for sentence in _sentences(text))


def _number_present(text: str, number: str) -> bool:
    # 避免把 30 匹配进 300、把 2 匹配进 2026。
    return re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", text or "") is not None


# ===================== 汇总 =====================


def aggregate(scores: Iterable[CaseScore]) -> dict[str, Any]:
    rows = list(scores)
    by_mode: dict[str, dict[str, Any]] = {}
    by_mode_category: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        mode = by_mode.setdefault(
            row.mode,
            {
                "runs": 0,
                "autoPass": 0,
                "failed": 0,
                "manual": 0,
                "answerApplicable": 0,
                "answerPassed": 0,
                "citationPassed": 0,
                "retrievalApplicable": 0,
                "retrievalPassed": 0,
                "contextApplicable": 0,
                "contextPassed": 0,
                "citations": 0,
                "illegalCitations": 0,
                "malformedCitations": 0,
                "unsupportedCitations": 0,
                "latencyMs": 0,
            },
        )
        mode["runs"] += 1
        mode["autoPass"] += 1 if row.auto_pass else 0
        mode["failed"] += 1 if (not row.auto_pass and not row.manual_review) else 0
        mode["manual"] += 1 if row.manual_review else 0
        mode["citationPassed"] += 1 if row.citation.passed else 0
        mode["latencyMs"] += row.latency_ms
        if row.retrieval.applicable:
            mode["retrievalApplicable"] += 1
            mode["retrievalPassed"] += 1 if row.retrieval.passed else 0
        if row.context.applicable:
            mode["contextApplicable"] += 1
            mode["contextPassed"] += 1 if row.context.passed else 0
        if row.answer.applicable:
            mode["answerApplicable"] += 1
            mode["answerPassed"] += 1 if row.answer.passed else 0
        detail = row.citation.detail
        mode["citations"] += int(detail.get("total", 0))
        mode["illegalCitations"] += int(detail.get("invalid", 0))
        mode["malformedCitations"] += len(detail.get("malformedLabels", []))
        mode["unsupportedCitations"] += len(detail.get("unsupportedClaims", []))

        bucket = by_mode_category.setdefault(row.mode, {}).setdefault(
            row.category,
            {"runs": 0, "autoPass": 0, "manual": 0, "answerApplicable": 0, "answerPassed": 0,
             "citationPassed": 0, "citations": 0, "illegalCitations": 0},
        )
        bucket["runs"] += 1
        bucket["autoPass"] += 1 if row.auto_pass else 0
        bucket["manual"] += 1 if row.manual_review else 0
        bucket["citationPassed"] += 1 if row.citation.passed else 0
        if row.answer.applicable:
            bucket["answerApplicable"] += 1
            bucket["answerPassed"] += 1 if row.answer.passed else 0
        bucket["citations"] += int(detail.get("total", 0))
        bucket["illegalCitations"] += int(detail.get("invalid", 0))

    for mode, data in by_mode.items():
        data["answerPassRate"] = _rate(data["answerPassed"], data["answerApplicable"])
        data["retrievalPassRate"] = _rate(data["retrievalPassed"], data["retrievalApplicable"])
        data["contextPassRate"] = _rate(data["contextPassed"], data["contextApplicable"])
        data["citationPassRate"] = _rate(data["citationPassed"], data["runs"])
        data["illegalCitationRate"] = _rate(data["illegalCitations"], data["citations"])
        data["unsupportedCitationRate"] = _rate(data["unsupportedCitations"], data["citations"])
        data["averageLatencyMs"] = round(data["latencyMs"] / data["runs"], 1) if data["runs"] else None

    for groups in by_mode_category.values():
        for data in groups.values():
            data["answerPassRate"] = _rate(data["answerPassed"], data["answerApplicable"])
            data["citationPassRate"] = _rate(data["citationPassed"], data["runs"])
            data["illegalCitationRate"] = _rate(data["illegalCitations"], data["citations"])

    return {
        "runs": len(rows),
        "modes": by_mode,
        "byModeCategory": by_mode_category,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)
