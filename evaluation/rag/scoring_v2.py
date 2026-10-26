"""v2 评分规则：修正已确认的同义误判，并拆开引用质量指标。

v1 的结论保留在 `dataset.jsonl` / `scoring.py` / 4.2 报告里，不覆盖。v2 只改规则，
不改模型输出：

1. **答案层**：用 `requiredTermGroups` 表达「同一事实的已核验同义组」，保留必要数字、
   关系绑定和禁止模式；无证据问题用 `admissionPatterns` 要求「否定 + 所问对象」，
   不再因为回答里出现一个「没有 / 无法」就放行。
2. **引用层**：把 v1 混在一起的「引用通过」拆成六个独立指标，每个都带
   `numerator / denominator / rate`。合法对比引用不再自动判失败，但仍计入
   `extra_valid_source_rate`；非法标签、格式错误、缺必需来源、禁止来源仍判失败。
3. **来源支持度**仍只是词面启发式线索，不参与通过判定，也不能冒充事实准确率。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.citations import CitationReport

from .models import AnswerObservation, ContextObservation, EvalCase, RetrievalObservation
from .scoring import (
    CaseScore,
    Check,
    LayerScore,
    _binding_present,
    _normalize,
    _number_present,
    _score_context,
    _score_retrieval,
)

_RULES_VERSION = "v2"


def score_case_v2(
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
    answer_score = _score_answer_v2(case, mode, answer)
    citation_score = _score_citation_v2(case, mode, citation_report)

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


# ===================== 答案事实 =====================


def _score_answer_v2(
    case: EvalCase, mode: str, answer: AnswerObservation
) -> LayerScore:
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
            detail={"admissionApplicable": False, "rulesVersion": _RULES_VERSION},
        )

    text = answer.text or ""
    norm = _normalize(text)
    if answer.error:
        return LayerScore(
            applicable=True,
            passed=False,
            checks=[Check("answer_generated", False, answer.error)],
            detail={"error": answer.error, "rulesVersion": _RULES_VERSION},
        )

    missing_terms = [term for term in case.required_terms if _normalize(term) not in norm]
    missing_groups = [
        list(group)
        for group in case.required_term_groups
        if not any(_normalize(term) in norm for term in group)
    ]
    group_hits = {
        " / ".join(group): [
            term for term in group if _normalize(term) in norm
        ]
        for group in case.required_term_groups
    }
    missing_bindings = [
        list(pair)
        for pair in case.required_bindings
        if not _binding_present(text, pair[0], pair[1])
    ]
    missing_numbers = [
        number for number in case.required_numbers if not _number_present(text, number)
    ]
    phrases_found = [
        phrase for phrase in case.required_phrases_any if _normalize(phrase) in norm
    ]
    phrases_ok = not case.required_phrases_any or bool(phrases_found)
    forbidden_hits = [
        pattern for pattern in case.forbidden_patterns if re.search(pattern, text)
    ]

    if case.admission_patterns:
        admission_found = [
            pattern for pattern in case.admission_patterns if re.search(pattern, text)
        ]
        admission_ok = bool(admission_found)
        admission_mode = "patterns"
    else:
        admission_found = [
            term for term in case.admission_terms_any if _normalize(term) in norm
        ]
        admission_ok = not case.admission_terms_any or bool(admission_found)
        admission_mode = "terms"

    checks = [
        Check("required_terms", not missing_terms, f"missing={missing_terms}"),
        Check(
            "required_term_groups",
            not missing_groups,
            f"missingGroups={missing_groups} hits={group_hits}",
        ),
        Check("required_bindings", not missing_bindings, f"missing={missing_bindings}"),
        Check("required_numbers", not missing_numbers, f"missing={missing_numbers}"),
        Check(
            "required_phrases_any",
            phrases_ok,
            f"expectedAny={list(case.required_phrases_any)} found={phrases_found}",
        ),
        Check("forbidden_patterns", not forbidden_hits, f"hits={forbidden_hits}"),
        Check(
            "admission_evidence",
            admission_ok,
            f"mode={admission_mode} found={admission_found}",
        ),
    ]
    passed = (
        not missing_terms
        and not missing_groups
        and not missing_bindings
        and not missing_numbers
        and phrases_ok
        and not forbidden_hits
        and admission_ok
    )
    return LayerScore(
        applicable=True,
        passed=passed,
        checks=checks,
        detail={
            "rulesVersion": _RULES_VERSION,
            "missingTerms": missing_terms,
            "missingTermGroups": missing_groups,
            "termGroupHits": group_hits,
            "missingBindings": missing_bindings,
            "missingNumbers": missing_numbers,
            "phrasesFound": phrases_found,
            "forbiddenHits": forbidden_hits,
            "admissionMode": admission_mode,
            "admissionFound": admission_found,
            "answerChars": len(text),
        },
    )


# ===================== 引用 =====================


@dataclass
class CitationMetrics:
    """一次运行的引用计数；汇总时全部用计数重新算比例，避免百分比平均。"""

    exact_labels: int = 0
    illegal_labels: int = 0
    malformed_labels: int = 0
    valid_labels: int = 0
    support_warnings: int = 0
    has_citation_requirement: bool = False
    required_missing: bool = False
    has_forbidden_rule: bool = False
    forbidden_hit: bool = False
    extra_valid_documents: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "exactLabels": self.exact_labels,
            "illegalLabels": self.illegal_labels,
            "malformedLabels": self.malformed_labels,
            "validLabels": self.valid_labels,
            "supportWarnings": self.support_warnings,
            "hasCitationRequirement": self.has_citation_requirement,
            "requiredMissing": self.required_missing,
            "hasForbiddenRule": self.has_forbidden_rule,
            "forbiddenHit": self.forbidden_hit,
            "extraValidDocuments": self.extra_valid_documents,
        }


def _score_citation_v2(
    case: EvalCase, mode: str, report: CitationReport
) -> LayerScore:
    all_cited_docs = sorted({citation.document_name for citation in report.citations})
    valid_docs = set(report.cited_documents)
    required = set(case.required_citation_documents)
    allowed = set(case.allowed_citation_documents)
    contrast = set(case.contrast_citation_documents)
    forbidden = set(case.forbidden_citation_documents)
    expected_valid = allowed | required | contrast

    missing_required = sorted(required - valid_docs)
    forbidden_hit = sorted(forbidden & set(all_cited_docs))
    # 禁止来源已经由 forbidden_source_rate 单独计数，不再算作「额外的合法对比来源」。
    extra_valid = (
        sorted(valid_docs - expected_valid - forbidden)
        if expected_valid
        else sorted(valid_docs - forbidden)
    )

    if mode == "plain":
        expectation = "forbidden_no_context"
        passed = report.total == 0 and not report.malformed_labels
    elif case.citation_expectation == "required":
        expectation = "required"
        # v2 不再因「引用了 allowed 之外的合法标签」判失败：那可能是合理的对比来源。
        # 真正需要失败的是：没有引用、非法标签、格式错误、缺必需来源、引用禁止来源。
        passed = (
            report.total > 0
            and report.invalid == 0
            and not report.malformed_labels
            and not missing_required
            and not forbidden_hit
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
        )

    support_counts = {"strong": 0, "weak": 0, "none": 0, "numeric_conflict": 0}
    unsupported: list[dict[str, Any]] = []
    for citation in report.citations:
        if not citation.valid:
            continue
        support_counts[citation.lexical_support] = (
            support_counts.get(citation.lexical_support, 0) + 1
        )
        if citation.lexical_support in {"none", "numeric_conflict"}:
            unsupported.append(
                {
                    "label": citation.raw,
                    "claim": citation.claim,
                    "support": citation.lexical_support,
                    "numericConflict": citation.numeric_conflict,
                }
            )

    extra_labels = [
        citation.raw
        for citation in report.citations
        if citation.valid and citation.document_name in set(extra_valid)
    ]
    metrics = CitationMetrics(
        exact_labels=report.total,
        illegal_labels=report.invalid,
        malformed_labels=len(report.malformed_labels),
        valid_labels=report.valid,
        support_warnings=len(unsupported),
        # plain 模式没有知识上下文，任何引用都不合法；「缺必需来源」在那里没有意义，
        # 不能把它算进 required_source_missing_rate 的分母。
        has_citation_requirement=bool(required) and mode != "plain",
        required_missing=bool(missing_required),
        has_forbidden_rule=bool(forbidden),
        forbidden_hit=bool(forbidden_hit),
        extra_valid_documents=len(extra_valid),
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
            "extra_valid_sources",
            None,
            f"extraDocuments={extra_valid} labels={extra_labels}（不计入失败）",
        ),
    ]
    return LayerScore(
        applicable=True,
        passed=passed,
        checks=checks,
        detail={
            "rulesVersion": _RULES_VERSION,
            "expectation": expectation,
            "total": report.total,
            "valid": report.valid,
            "invalid": report.invalid,
            "invalidLabels": report.invalid_labels,
            "malformedLabels": report.malformed_labels,
            "duplicateLabels": report.duplicate_labels,
            "providedLabels": report.provided_labels,
            "citedDocuments": all_cited_docs,
            "validCitedDocuments": sorted(valid_docs),
            "requiredDocuments": sorted(required),
            "missingRequiredDocuments": missing_required,
            "forbiddenDocumentHits": forbidden_hit,
            "contrastDocuments": sorted(contrast),
            "extraValidDocuments": extra_valid,
            "extraValidLabels": extra_labels,
            "allowedDocuments": sorted(allowed),
            "supportCounts": support_counts,
            "unsupportedClaims": unsupported,
            "citations": [citation.as_dict() for citation in report.citations],
            "metrics": metrics.as_dict(),
        },
    )


# ===================== 汇总 =====================


def aggregate_v2(scores: Iterable[CaseScore]) -> dict[str, Any]:
    rows = list(scores)
    by_mode: dict[str, dict[str, Any]] = {}
    by_mode_category: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        mode = by_mode.setdefault(
            row.mode,
            _empty_bucket(include_retrieval=True),
        )
        _add_row(mode, row)
        bucket = by_mode_category.setdefault(row.mode, {}).setdefault(
            row.category, _empty_bucket(include_retrieval=False)
        )
        _add_row(bucket, row, by_category=True)

    for data in by_mode.values():
        _finish_bucket(data, include_retrieval=True)
    for groups in by_mode_category.values():
        for data in groups.values():
            _finish_bucket(data, include_retrieval=False)

    return {
        "rulesVersion": _RULES_VERSION,
        "runs": len(rows),
        "modes": by_mode,
        "byModeCategory": by_mode_category,
    }


def _empty_bucket(*, include_retrieval: bool) -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "runs": 0,
        "autoPass": 0,
        "failed": 0,
        "manual": 0,
        "answerApplicable": 0,
        "answerPassed": 0,
        "citationPassed": 0,
        "latencyMs": 0,
        "exactLabels": 0,
        "illegalLabels": 0,
        "malformedLabels": 0,
        "validLabels": 0,
        "supportWarnings": 0,
        "citationRequirementRuns": 0,
        "requiredMissingRuns": 0,
        "forbiddenRuleRuns": 0,
        "forbiddenHitRuns": 0,
        "validCitationRuns": 0,
        "extraValidRuns": 0,
    }
    if include_retrieval:
        bucket.update(
            {
                "retrievalApplicable": 0,
                "retrievalPassed": 0,
                "contextApplicable": 0,
                "contextPassed": 0,
            }
        )
    return bucket


def _add_row(bucket: dict[str, Any], row: CaseScore, *, by_category: bool = False) -> None:
    bucket["runs"] += 1
    bucket["autoPass"] += 1 if row.auto_pass else 0
    bucket["failed"] += 1 if (not row.auto_pass and not row.manual_review) else 0
    bucket["manual"] += 1 if row.manual_review else 0
    bucket["citationPassed"] += 1 if row.citation.passed else 0
    bucket["latencyMs"] += row.latency_ms
    if row.answer.applicable:
        bucket["answerApplicable"] += 1
        bucket["answerPassed"] += 1 if row.answer.passed else 0
    if not by_category:
        if row.retrieval.applicable:
            bucket["retrievalApplicable"] += 1
            bucket["retrievalPassed"] += 1 if row.retrieval.passed else 0
        if row.context.applicable:
            bucket["contextApplicable"] += 1
            bucket["contextPassed"] += 1 if row.context.passed else 0

    metrics = row.citation.detail.get("metrics", {})
    bucket["exactLabels"] += int(metrics.get("exactLabels", 0))
    bucket["illegalLabels"] += int(metrics.get("illegalLabels", 0))
    bucket["malformedLabels"] += int(metrics.get("malformedLabels", 0))
    bucket["validLabels"] += int(metrics.get("validLabels", 0))
    bucket["supportWarnings"] += int(metrics.get("supportWarnings", 0))
    if metrics.get("hasCitationRequirement"):
        bucket["citationRequirementRuns"] += 1
        bucket["requiredMissingRuns"] += 1 if metrics.get("requiredMissing") else 0
    if metrics.get("hasForbiddenRule"):
        bucket["forbiddenRuleRuns"] += 1
        bucket["forbiddenHitRuns"] += 1 if metrics.get("forbiddenHit") else 0
    if int(metrics.get("validLabels", 0)) > 0:
        bucket["validCitationRuns"] += 1
        bucket["extraValidRuns"] += (
            1 if int(metrics.get("extraValidDocuments", 0)) > 0 else 0
        )


def _finish_bucket(bucket: dict[str, Any], *, include_retrieval: bool) -> None:
    runs = bucket["runs"]
    bucket["answerPassRate"] = _rate(bucket["answerPassed"], bucket["answerApplicable"])
    bucket["citationPassRate"] = _rate(bucket["citationPassed"], runs)
    bucket["averageLatencyMs"] = round(bucket["latencyMs"] / runs, 1) if runs else None
    if include_retrieval:
        bucket["retrievalPassRate"] = _rate(
            bucket["retrievalPassed"], bucket["retrievalApplicable"]
        )
        bucket["contextPassRate"] = _rate(
            bucket["contextPassed"], bucket["contextApplicable"]
        )

    label_occurrences = bucket["exactLabels"] + bucket["malformedLabels"]
    bucket["metrics"] = {
        "illegal_label_rate": _metric(
            bucket["illegalLabels"],
            bucket["exactLabels"],
            "非法精确标签 / 精确标签总数",
        ),
        "malformed_label_rate": _metric(
            bucket["malformedLabels"],
            label_occurrences,
            "格式错误标签 / 全部标签出现次数（精确 + 格式错误）",
        ),
        "required_source_missing_rate": _metric(
            bucket["requiredMissingRuns"],
            bucket["citationRequirementRuns"],
            "缺必需来源的运行 / 有必需来源要求的运行",
        ),
        "forbidden_source_rate": _metric(
            bucket["forbiddenHitRuns"],
            bucket["forbiddenRuleRuns"],
            "引用了禁止来源的运行 / 配置了禁止来源的运行",
        ),
        "extra_valid_source_rate": _metric(
            bucket["extraValidRuns"],
            bucket["validCitationRuns"],
            "含额外合法来源的运行 / 至少有一条合法引用的运行",
        ),
        "citation_support_warning_rate": _metric(
            bucket["supportWarnings"],
            bucket["validLabels"],
            "来源支持度为 none/numeric_conflict 的引用 / 合法引用数",
        ),
        "citation_pass_rate": _metric(
            bucket["citationPassed"],
            runs,
            "引用层通过的运行 / 全部运行（分母是运行数，不是标签数）",
        ),
    }


def _metric(numerator: int, denominator: int, definition: str) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": _rate(numerator, denominator),
        "definition": definition,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)
