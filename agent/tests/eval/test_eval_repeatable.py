"""评测脚本本身的回归：每个用例都有断言，且同一数据集两次运行结果一致。"""

from __future__ import annotations

from tests.eval.runner import CASES, deterministic_projection, run_eval


def test_all_eval_cases_have_assertions_and_pass() -> None:
    report = run_eval(repeats=1, write_report=False)

    for mode, result in report["modes"].items():
        assert result["passed"], f"{mode} 存在失败用例"
        assert len(result["cases"]) == len(CASES)
        for case in result["cases"]:
            assert case["failures"] == []
            # 每个用例至少要断言 current / expanded / Resolver 调用决策。
            assert {"current", "expanded", "resolverCalled"} <= set(case["observed"])
    assert report["summary"]["allPassed"] is True


def test_eval_is_repeatable() -> None:
    first = run_eval(repeats=1, write_report=False)
    second = run_eval(repeats=1, write_report=False)

    assert deterministic_projection(first) == deterministic_projection(second)


def test_case_coverage_matches_required_scenarios() -> None:
    ids = {case.case_id for case in CASES}
    assert {
        "prereq-derivative-limit",
        "related-continuity-limit",
        "tcp-congestion",
        "pagetable-tlb",
        "synonym-derivative-basics",
        "fuzzy-anaphora",
        "cross-space-isolation",
        "chitchat-short-message",
        "rule-hit-no-resolver",
    } <= ids
