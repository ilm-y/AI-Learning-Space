"""按 Phase 3.1 规则重新判读已保存的真实模型 artifact。

Phase 3 的 artifact 只保存了当时的原始字段；新增的完整性状态、掌握断言方向、
引用语法统计和 Evidence Guard 最终披露结果是**派生字段**，没有写回历史文件。
本模块在内存中补齐这些字段，再用当前硬断言重新判读，供报告与人工复核表使用。

它不调用模型，也不修改 artifact 文件；所有结论都可从原始回答文本重算。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.citations import validate_citations
from app.evidence_guard import already_discloses, status_notice
from app.schemas import KnowledgeChunk
from evaluation.phase1.claims import analyze_mastery_claim
from evaluation.phase1.completion import evaluate_completion
from evaluation.phase1.executors import ExecOutcome
from evaluation.phase1.models import EvalCase
from evaluation.phase1.scoring import run_hard_assertions
from evaluation.phase3.runner import _apply_completion_to_semantics, _status_from_outcome


def enrich_actual(actual: dict[str, Any]) -> dict[str, Any]:
    """返回补齐派生字段的副本；原始 artifact 不变。"""
    enriched = copy.deepcopy(actual)
    answer = enriched.setdefault("answer", {})
    text = str(answer.get("text") or "")

    answer.setdefault("masteryClaim", analyze_mastery_claim(text))

    citations = answer.get("citations")
    if isinstance(citations, dict):
        malformed = citations.get("malformedLabels") or []
        citations.setdefault("malformedCount", len(malformed))
        citations.setdefault("syntaxValid", len(malformed) == 0)
        citations.setdefault(
            "unsupportedCount",
            sum(
                1
                for item in citations.get("citations") or []
                if item.get("valid") and item.get("lexicalSupport") == "none"
            ),
        )

    guard = answer.get("evidenceGuard")
    if isinstance(guard, dict) and guard.get("status"):
        notice = status_notice(str(guard["status"]))
        final_discloses = already_discloses(str(guard["status"]), text) or bool(
            notice and notice in text
        )
        guard.setdefault("finalDiscloses", final_discloses)
        guard.setdefault(
            "duplicateNotice", bool(notice) and text.count(notice) > 1
        )
        guard.setdefault(
            "addedOrDisclosed", bool(guard.get("added")) or final_discloses
        )
        guard.setdefault(
            "outcome",
            "added"
            if guard.get("added")
            else ("already_disclosed" if final_discloses else "missing"),
        )
    return enriched


def reclassify_artifact(
    artifact: dict[str, Any],
    case: EvalCase,
    *,
    chunks: list[KnowledgeChunk] | None = None,
) -> dict[str, Any]:
    """用当前规则给出 case/completion 状态与逐条断言结果。

    ``chunks`` 只用于需要重新解析引用标签的调用；artifact 里已经保存了
    ``citations`` 时以保存结果为准，避免用事后构造的片段改变原判。
    """
    actual = enrich_actual(artifact.get("actual") or {})
    answer = actual.get("answer") or {}
    if chunks is not None and not answer.get("citations"):
        answer["citations"] = validate_citations(
            str(answer.get("text") or ""), chunks
        ).as_dict()

    completion = evaluate_completion(
        actual.get("llm") or {}, str(answer.get("text") or ""), artifact.get("error")
    )
    checks = run_hard_assertions(case, actual)
    status, reason = _status_from_outcome(
        ExecOutcome(actual=actual), checks, completion
    )

    semantics = copy.deepcopy(artifact.get("semantics") or {})
    if semantics:
        _apply_completion_to_semantics(semantics, completion)

    return {
        "caseId": artifact.get("caseId"),
        "runIndex": artifact.get("runIndex"),
        "caseStatus": status,
        "caseReason": reason,
        "recordedHardStatus": artifact.get("hardStatus"),
        "completion": completion,
        "checks": checks,
        "actual": actual,
        "semantics": semantics,
    }


def default_artifact_paths(root: Path) -> list[Path]:
    """Phase 3 全部真实运行 artifact，按路径稳定排序。"""
    return sorted((root / "evaluation" / "phase3" / "artifacts").glob("phase*/*.json"))
