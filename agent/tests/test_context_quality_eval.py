"""D4-5 Context Quality Evaluation 的 pytest 包装。

CLI 面向本地排查与 CI 日志，pytest 面向回归门禁。两者复用同一份数据集与执行器，
不重复实现断言。
"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.eval.context_quality.dataset import (
    CASES,
    CATEGORY_ORDER,
    cases_by_category,
)
from tests.eval.context_quality.harness import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    METRIC_KEYS,
    compare_baselines,
    run_all_cases,
    run_baselines,
)
from tests.eval.context_quality.probes import (
    probe_budget_all_dropped,
    probe_protected_overflow,
    probe_redundancy_threshold,
    probe_session_mismatch,
)
from tests.eval.context_quality.run_context_quality_eval import main


def test_dataset_covers_required_categories() -> None:
    assert len(CATEGORY_ORDER) >= 9
    grouped = cases_by_category()
    assert set(grouped) == set(CATEGORY_ORDER)
    for category in CATEGORY_ORDER:
        assert len(grouped[category]) >= 2, category

    ids = [case.case_id for case in CASES]
    assert len(ids) == len(set(ids)), "case_id must be unique"
    assert all(case.description for case in CASES)
    assert all(case.expected.retrieval for case in CASES)
    assert all(
        case.expected.injection or case.expected.must_not_inject
        for case in CASES
    )


def test_dataset_declares_injection_reasons() -> None:
    seen_statuses: set[str] = set()
    for case in CASES:
        for source_id, status in case.expected.injection.items():
            seen_statuses.add(status)
            assert (
                source_id in case.expected.injection_reasons
                or status == "SELECTED"
            ), (case.case_id, source_id, status)
        assert set(case.expected.injection_reasons) <= set(
            case.expected.injection
        ), case.case_id

    # 四种注入结果都要有实际 case 覆盖，避免只测「正常选中」。
    assert {"SELECTED", "TRUNCATED", "OMITTED", "EXCLUDED"} <= seen_statuses


def test_dataset_declares_failure_semantics() -> None:
    degraded = [case for case in CASES if case.category == "degraded_retrieval"]
    assert degraded
    for case in degraded:
        assert case.expected.degraded_sources
        assert case.expected.forbidden_text, "must forbid false-empty claims"

    overflow = [
        case for case in CASES if case.expected.allow_known_overflow
    ]
    assert overflow, "protected overflow must be represented explicitly"
    assert all(
        case.max_context_tokens < 100 for case in overflow
    )


def test_current_pipeline_passes_all_cases() -> None:
    results = run_all_cases()
    failures = {
        result.case_id: result.failures
        for result in results
        if not result.passed
    }
    assert not failures, failures
    assert all(result.passed for result in results)


def test_evaluation_is_deterministic() -> None:
    def snapshot():
        return {
            result.case_id: (
                result.passed,
                tuple(result.failures),
                tuple(sorted(result.metrics.items())),
                result.context.context_manifest,
                result.context.system_instruction,
            )
            for result in run_all_cases()
        }

    assert snapshot() == snapshot()


def test_c_is_no_worse_than_baselines() -> None:
    comparison = compare_baselines(run_baselines())["comparison"]
    assert set(comparison) == set(METRIC_KEYS)
    for key, item in comparison.items():
        if key in HIGHER_IS_BETTER:
            assert item["C"] + 1e-9 >= item["A"], key
            assert item["C"] + 1e-9 >= item["B"], key
        elif key in LOWER_IS_BETTER:
            assert item["C"] <= item["A"] + 1e-9, key
            assert item["C"] <= item["B"] + 1e-9, key
        else:
            assert item["criterion"] == "report_only", key


def test_known_risk_budget_all_dropped() -> None:
    probe = probe_budget_all_dropped()
    assert probe.ok
    assert probe.observations["knowledge_state"] == "SUCCESS_BUT_DROPPED"
    assert probe.observations["status_note_present"] is True
    assert probe.observations["false_no_retrieval_claim"] is False
    assert probe.observations["dropped_chunk_ids"] == [9901]


def test_known_risk_session_mismatch() -> None:
    probe = probe_session_mismatch()
    assert probe.ok
    assert probe.observations["session_reason"] == "session_concept_mismatch"
    assert probe.observations["inject_goal"] is False
    assert probe.observations["inject_current_focus"] is False
    assert probe.observations["stale_markers_in_prompt"] == []
    assert probe.observations["session_entry_statuses"] == []


def test_known_risk_protected_overflow() -> None:
    probe = probe_protected_overflow()
    assert probe.ok
    assert probe.observations["used"] > probe.observations["effective"]
    assert probe.observations["budget_exceeded"] is True
    assert probe.observations["overrun_reason"]
    assert probe.observations["degraded_status_still_visible"] is True


def test_known_risk_redundancy_threshold() -> None:
    probe = probe_redundancy_threshold()
    observations = probe.observations
    assert probe.ok

    # 改写漏判：保守偏置，只影响预算，不丢内容。
    assert observations["paraphrase_detected_as_redundant"] is False
    assert observations["paraphrase_tier"] == "full"
    assert observations["paraphrase_overlap"] < 0.6

    # 语义相反但字符重合高：已复现的误判，且 structured 截断丢掉否定词。
    assert observations["negation_detected_as_redundant"] is True
    assert observations["negation_overlap"] >= 0.6
    assert observations["negation_tier"] == "structured"
    assert observations["negation_token_lost_in_compressed_text"] is True


def test_cli_exit_code_and_json(tmp_path, capsys) -> None:
    report_path = tmp_path / "d4-5.json"
    assert main(["--json", str(report_path)]) == 0
    captured = capsys.readouterr()
    assert "CI status: PASS" in captured.out
    assert report_path.exists()

    import json

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dataset"]["cases"] == len(CASES)
    assert len(report["cases"]) == len(CASES)
    assert all(item["passed"] for item in report["cases"])
    assert report["probes"]
    assert report["probes"][0]["name"] == "budget_all_dropped"

    category_counts = Counter(
        item["category"] for item in report["cases"]
    )
    assert set(category_counts) == set(CATEGORY_ORDER)
