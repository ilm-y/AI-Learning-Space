"""RAG 评测的数据结构。

用例是数据（`dataset.jsonl`），观测是 runner 采到的原始事实，评分是
`scoring.py` 根据两者算出来的。三者分开，reviewer 才能区分：

- 检索没找到（retrieval）
- 找到了但没进上下文（context）
- 进了上下文但模型答错（answer）
- 答对了但引用不合法 / 来源不支持（citation）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CATEGORIES = ("A", "B", "C", "D", "E", "F")

CATEGORY_LABELS = {
    "A": "单片段直接事实",
    "B": "相似主题/不同事实",
    "C": "多片段综合",
    "D": "无证据问题",
    "E": "冲突来源",
    "F": "检索降级",
}

MODES = ("plain", "rag", "rag+state")

CITATION_EXPECTATIONS = ("required", "forbidden", "optional")

SCENARIOS = ("main", "empty_space", "qdrant_down", "embedding_down")

STATUSES = (
    "SUCCESS",
    "NO_VALID_DOCUMENTS",
    "NO_MATCH",
    "THRESHOLD_FILTERED",
    "DEGRADED",
    "NOT_RETRIEVED",
)


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    question: str
    expected_points: tuple[str, ...]
    expected_status: tuple[str, ...]
    expected_documents: tuple[str, ...]
    citation_expectation: str
    allowed_citation_documents: tuple[str, ...]
    required_citation_documents: tuple[str, ...]
    forbidden_citation_documents: tuple[str, ...]
    required_terms: tuple[str, ...]
    required_bindings: tuple[tuple[str, str], ...]
    required_numbers: tuple[str, ...]
    required_phrases_any: tuple[str, ...]
    forbidden_patterns: tuple[str, ...]
    admission_terms_any: tuple[str, ...]
    modes: tuple[str, ...]
    scenario: str
    manual_review: bool
    notes: str
    # ---- v2 规则的可选字段；v1 用例不填时行为完全不变 ----
    # 每组内任一说法命中即可，所有组都要命中。用来表达「同一事实的已人工核验同义说法」，
    # 比要求两个词严格同句更稳，也避免用无限扩张的关键词列表掩盖语义问题。
    required_term_groups: tuple[tuple[str, ...], ...] = ()
    # 无证据问题的「承认资料不足」正则。比 admissionTermsAny 更精确：
    # 必须同时出现否定与所问对象，不能因为回答里随便出现「没有」「无法」就放行。
    admission_patterns: tuple[str, ...] = ()
    # 允许额外引用的对比来源：合法标签、不是必需来源、也不因「不在 allowed 里」判错。
    # 它们仍会进入 extra_valid_source_rate，便于人工分辨「合法对比」与「乱引」。
    contrast_citation_documents: tuple[str, ...] = ()

    def applies_to(self, mode: str) -> bool:
        return mode in self.modes

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "question": self.question,
            "expectedPoints": list(self.expected_points),
            "expectedStatus": list(self.expected_status),
            "expectedDocuments": list(self.expected_documents),
            "citationExpectation": self.citation_expectation,
            "allowedCitationDocuments": list(self.allowed_citation_documents),
            "requiredCitationDocuments": list(self.required_citation_documents),
            "forbiddenCitationDocuments": list(self.forbidden_citation_documents),
            "requiredTerms": list(self.required_terms),
            "requiredBindings": [list(pair) for pair in self.required_bindings],
            "requiredNumbers": list(self.required_numbers),
            "requiredPhrasesAny": list(self.required_phrases_any),
            "forbiddenPatterns": list(self.forbidden_patterns),
            "admissionTermsAny": list(self.admission_terms_any),
            "modes": list(self.modes),
            "scenario": self.scenario,
            "manualReview": self.manual_review,
            "notes": self.notes,
            "requiredTermGroups": [list(group) for group in self.required_term_groups],
            "admissionPatterns": list(self.admission_patterns),
            "contrastCitationDocuments": list(self.contrast_citation_documents),
        }


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    document_name: str
    chunk_index: int
    score: float
    content: str
    heading_path: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"资料《{self.document_name}》#{self.chunk_index}"

    def as_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chunkId": self.chunk_id,
            "documentId": self.document_id,
            "documentName": self.document_name,
            "chunkIndex": self.chunk_index,
            "score": self.score,
            "label": self.label,
            "headingPath": list(self.heading_path),
        }
        if include_content:
            data["content"] = self.content
        return data


@dataclass
class RetrievalObservation:
    status: str = "NOT_RETRIEVED"
    warning: str | None = None
    chunks: list[RetrievedChunk] = field(default_factory=list)
    candidates: int | None = None
    duration_ms: int = 0
    collection: str | None = None
    error: str | None = None
    allowlist_size: int | None = None

    @property
    def document_names(self) -> list[str]:
        names: list[str] = []
        for chunk in self.chunks:
            if chunk.document_name not in names:
                names.append(chunk.document_name)
        return names

    def as_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "warning": self.warning,
            "candidates": self.candidates,
            "durationMs": self.duration_ms,
            "collection": self.collection,
            "allowlistSize": self.allowlist_size,
            "error": self.error,
            "documents": self.document_names,
            "chunks": [
                chunk.as_dict(include_content=include_content) for chunk in self.chunks
            ],
        }


@dataclass
class ContextObservation:
    mode: str
    text: str = ""
    provided_labels: list[str] = field(default_factory=list)
    context_documents: list[str] = field(default_factory=list)
    context_chunk_ids: list[int] = field(default_factory=list)
    dropped_knowledge: int = 0
    estimated_tokens: int = 0
    detected_concepts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode,
            "providedLabels": self.provided_labels,
            "contextDocuments": self.context_documents,
            "contextChunkIds": self.context_chunk_ids,
            "droppedKnowledge": self.dropped_knowledge,
            "estimatedTokens": self.estimated_tokens,
            "detectedConcepts": self.detected_concepts,
            "warnings": self.warnings,
            "sections": self.sections,
        }
        if include_text:
            data["text"] = self.text
        return data


@dataclass
class AnswerObservation:
    text: str = ""
    latency_ms: int = 0
    error: str | None = None
    provider: str = ""
    model: str = ""
    temperature: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "latencyMs": self.latency_ms,
            "error": self.error,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
        }


# ===================== 加载 =====================


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{line_no} 不是合法 JSON：{exc}") from exc
        cases.append(_parse_case(item, f"{path.name}:{line_no}"))
    _assert_unique(cases)
    return cases


def _parse_case(item: dict[str, Any], where: str) -> EvalCase:
    for key in ("id", "category", "question"):
        if not item.get(key):
            raise ValueError(f"{where}: 缺少必填字段 {key}")
    category = str(item["category"])
    if category not in CATEGORIES:
        raise ValueError(f"{where}: 未知 category {category!r}")
    expected_status = _as_tuple(item.get("expectedStatus", "SUCCESS"))
    for status in expected_status:
        if status not in STATUSES:
            raise ValueError(f"{where}: 未知 expectedStatus {status!r}")
    citation = str(item.get("citationExpectation", "required"))
    if citation not in CITATION_EXPECTATIONS:
        raise ValueError(f"{where}: 未知 citationExpectation {citation!r}")
    modes = _as_tuple(item.get("modes", MODES))
    for mode in modes:
        if mode not in MODES:
            raise ValueError(f"{where}: 未知 mode {mode!r}")
    scenario = str(item.get("scenario", "main"))
    if scenario not in SCENARIOS:
        raise ValueError(f"{where}: 未知 scenario {scenario!r}")

    bindings = tuple(
        (str(pair[0]), str(pair[1]))
        for pair in item.get("requiredBindings", [])
        if isinstance(pair, list) and len(pair) == 2
    )
    term_groups = tuple(
        tuple(str(term) for term in group)
        for group in item.get("requiredTermGroups", [])
        if isinstance(group, list) and group
    )
    return EvalCase(
        id=str(item["id"]),
        category=category,
        question=str(item["question"]),
        expected_points=_as_tuple(item.get("expectedPoints", [])),
        expected_status=expected_status,
        expected_documents=_as_tuple(item.get("expectedDocuments", [])),
        citation_expectation=citation,
        allowed_citation_documents=_as_tuple(item.get("allowedCitationDocuments", [])),
        required_citation_documents=_as_tuple(item.get("requiredCitationDocuments", [])),
        forbidden_citation_documents=_as_tuple(item.get("forbiddenCitationDocuments", [])),
        required_terms=_as_tuple(item.get("requiredTerms", [])),
        required_bindings=bindings,
        required_numbers=_as_tuple(item.get("requiredNumbers", [])),
        required_phrases_any=_as_tuple(item.get("requiredPhrasesAny", [])),
        forbidden_patterns=_as_tuple(item.get("forbiddenPatterns", [])),
        admission_terms_any=_as_tuple(item.get("admissionTermsAny", [])),
        modes=modes,
        scenario=scenario,
        manual_review=bool(item.get("manualReview", False)),
        notes=str(item.get("notes", "")),
        required_term_groups=term_groups,
        admission_patterns=_as_tuple(item.get("admissionPatterns", [])),
        contrast_citation_documents=_as_tuple(item.get("contrastCitationDocuments", [])),
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _assert_unique(cases: list[EvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"用例 id 重复：{case.id}")
        seen.add(case.id)
