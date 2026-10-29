"""Phase 3.1 评测器校准回归：用已有真实模型 artifact 复现旧误判。

这些测试不调用真实模型；它们读取 Phase 3 的真实输出，验证：

- B05-r1 / E01-r2 的 finish_reason=length 会被识别为 TRUNCATED，而不是硬 PASS；
- B05 不再要求 Guard 必须 added（模型已说明时不重复追加才是正确的）；
- E01 的否定/历史/问句不会被关键词 not_contains 误判；
- D01 的 malformed 标注不会因为 invalid=0 被放过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.citations import validate_citations
from app.evidence_guard import already_discloses, evidence_status_notice, status_notice
from app.schemas import KnowledgeChunk
from evaluation.phase1.claims import analyze_mastery_claim
from evaluation.phase1.completion import (
    COMPLETE,
    EMPTY,
    TRUNCATED,
    UNKNOWN,
    evaluate_completion,
)
from evaluation.phase1.executors import ExecOutcome
from evaluation.phase1.models import load_cases
from evaluation.phase1.scoring import run_hard_assertions
from evaluation.phase3.reclassify import enrich_actual, reclassify_artifact
from evaluation.phase3.runner import _apply_completion_to_semantics, _status_from_outcome

ARTIFACTS = REPO_ROOT / "evaluation" / "phase3" / "artifacts"
CASES_PATH = REPO_ROOT / "docs" / "testing" / "phase3-agent-quality-cases.jsonl"


def _artifact(relative: str) -> dict:
    return json.loads((ARTIFACTS / relative).read_text(encoding="utf-8"))


def _reclassify(relative: str) -> dict:
    artifact = _artifact(relative)
    return reclassify_artifact(artifact, _case(artifact["caseId"]))


def _case(case_id: str):
    return {case.id: case for case in load_cases(CASES_PATH, phase1_fixed_set=False)}[case_id]


def test_b05_r1_content_passes_but_truncation_forces_fail() -> None:
    result = _reclassify("phase3-baseline-20261027/B05-r1.json")
    completion = result["completion"]
    assert completion["status"] == TRUNCATED
    assert completion["finishReason"] == "length"

    # 旧报告把「Guard 没有追加」和「措辞不同」当成内容失败；补齐派生字段后，
    # 内容断言全部通过，但任务仍因截断被独立判为 FAIL。
    assert all(check.passed for check in result["checks"])
    assert result["caseStatus"] == "FAIL"
    assert "截断" in result["caseReason"] and "length" in result["caseReason"]


def test_b05_already_disclosed_is_valid_guard_outcome() -> None:
    data = _artifact("phase3-baseline-20261027/B05-r1.json")
    text = data["actual"]["answer"]["text"]
    assert already_discloses("NO_MATCH", text) is True
    # 模型已说明时 Guard 不追加，不是失败。
    assert evidence_status_notice("NO_MATCH", "解释一下 TLB 和缺页的区别。", text) is None
    assert status_notice("NO_MATCH") not in (None, "")


def test_b05_guard_duplicate_notice_is_detected() -> None:
    notice = status_notice("NO_MATCH")
    assert notice
    text = "TLB 是页表项缓存。" + notice + notice
    assert text.count(notice) == 2


def test_e01_r2_is_truncated_and_cannot_keep_old_pass() -> None:
    data = _artifact("phase3-repeats-20261027/E01-r2.json")
    actual = data["actual"]
    completion = evaluate_completion(
        actual.get("llm") or {}, actual["answer"]["text"], None
    )
    assert completion["status"] == TRUNCATED
    checks = run_hard_assertions(_case("E01"), actual)
    status, reason = _status_from_outcome(
        ExecOutcome(actual=actual), checks, completion
    )
    assert status == "FAIL"
    assert "截断" in reason


def test_e01_negation_artifact_is_no_longer_false_positive() -> None:
    # E01-r3 历史上因关键词 not_contains 被误判 FAIL；新断言看语义方向，应 PASS。
    result = _reclassify("phase3-repeats-20261027/E01-r3.json")
    assert result["recordedHardStatus"] == "FAIL"
    assert result["caseStatus"] == "PASS"
    mastery = result["actual"]["answer"]["masteryClaim"]
    assert mastery["affirmativeCurrent"] is False
    assert mastery["negatedCount"] == 1

    # r1 同样是否定表述，也应通过；r2 内容通过但被截断，仍是 FAIL。
    assert _reclassify("phase3-repeats-20261027/E01-r1.json")["caseStatus"] == "PASS"
    truncated = _reclassify("phase3-repeats-20261027/E01-r2.json")
    assert truncated["completion"]["status"] == TRUNCATED
    assert truncated["caseStatus"] == "FAIL"


def test_d01_malformed_label_fails_despite_invalid_zero() -> None:
    result = _reclassify("phase3-baseline-20261027/D01-r1.json")
    citations = result["actual"]["answer"]["citations"]
    assert citations["invalid"] == 0
    assert citations["malformedCount"] == 2
    assert citations["syntaxValid"] is False
    assert result["caseStatus"] == "FAIL"


def test_truncation_clamps_task_completion_score() -> None:
    semantics = {
        "dimensions": [
            {"id": "task_completion", "score": 2, "reason": "看起来回答了"},
            {"id": "factual_correctness", "score": 2, "reason": "正确"},
        ],
        "failureTags": [],
        "requiresHumanReview": False,
    }
    _apply_completion_to_semantics(semantics, {"status": TRUNCATED})
    by_id = {item["id"]: item for item in semantics["dimensions"]}
    assert by_id["task_completion"]["score"] == 1
    assert by_id["task_completion"]["completionAdjusted"] is True
    assert by_id["factual_correctness"]["score"] == 2
    assert semantics["requiresHumanReview"] is True
    assert TRUNCATED in semantics["failureTags"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("你已经完全掌握。", True),
        ("你已经完全掌握了。", True),
        ("现在还不能说你已经完全掌握。", False),
        ("现在还不能说你已经完全掌握了。", False),
        ("虽然做对了这题，但证据不足以确认已经完全掌握。", False),
        ("历史上曾判断为完全掌握，但当前证据还不足。", False),
        ("目前已有充分证据表明你掌握了核心概念，但仍需验证边界情况。", False),
        ("你现在已经完全掌握了吗？", False),
        ("还不到「完全掌握」的程度。", False),
    ],
)
def test_mastery_direction_separates_affirmation_negation_history_question(
    text: str, expected: bool
) -> None:
    assert analyze_mastery_claim(text)["affirmativeCurrent"] is expected


def test_completion_status_is_independent_of_content_quality() -> None:
    llm = {"diagnosticFinishReasons": ["length"], "callCount": 1}
    result = evaluate_completion(llm, "这是一段看起来非常正确的完整回答。" * 5)
    assert result["status"] == TRUNCATED
    assert evaluate_completion(llm, "")["status"] == EMPTY
    stopped = evaluate_completion(
        {"diagnosticFinishReasons": ["stop"]}, "这是一段完整但可能错误的回答。"
    )
    assert stopped["status"] == COMPLETE
    unknown = evaluate_completion({}, "没有 finish_reason 但有内容。" * 3)
    assert unknown["status"] == UNKNOWN


def _chunk(index: int = 0) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=1501,
        document_id=1,
        chunk_index=index,
        document_name="tlb.md",
        file_type="md",
        content="TLB Miss 只是快表没有命中，还需要查页表；页表项表明页不在内存才是缺页。",
        score=0.9,
    )


def test_d01_malformed_label_cannot_pass_because_invalid_is_zero() -> None:
    chunks = [_chunk()]
    legal = "TLB Miss 不等于缺页。[资料《tlb.md》#0]"
    mixed = "TLB Miss 不等于缺页。[资料《tlb.md》#0] 另外参见 [#1]。"
    illegal_index = "TLB Miss 不等于缺页。[资料《tlb.md》#9]"
    no_label = "TLB Miss 不等于缺页。"

    report = validate_citations(legal, chunks)
    assert report.invalid == 0 and report.malformed_count == 0
    assert report.syntax_valid is True and report.valid == 1

    report = validate_citations(mixed, chunks)
    assert report.invalid == 0  # 旧断言会误以为通过
    assert report.malformed_count == 1
    assert report.syntax_valid is False

    report = validate_citations(illegal_index, chunks)
    assert report.invalid == 1 and report.syntax_valid is True

    report = validate_citations(no_label, chunks)
    assert report.valid == 0 and report.syntax_valid is True


def test_d01_syntax_legality_is_not_factual_support() -> None:
    chunks = [_chunk()]
    # 格式和索引都合法，但句子与片段没有任何词面重合：应进入 unsupportedCount，
    # 而不是被当成「引用合法 = 事实正确」。
    text = "宙斯盾系统使用量子密钥分发。[资料《tlb.md》#0]"
    report = validate_citations(text, chunks)
    assert report.invalid == 0 and report.malformed_count == 0
    assert report.unsupported_count == 1
