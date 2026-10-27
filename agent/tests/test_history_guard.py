"""复盘回答的证据约束。

这里钉死的是「断言是否被记录支撑」，不是某个失败句子的关键词：
- 记录原文里出现过的内省表述可以引用；
- 没有记录支撑的心理过程 / 外部资料使用会被识别；
- PARTIAL 不能升级成绝对判断，证据条数不能概括成「唯一」；
- 没有历史轨迹时不能否认过去，也不能从当前状态反推。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.history_guard import (
    build_history_fallback,
    check_history_answer,
    retry_instruction,
)
from app.schemas import (
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceSourceItem,
    EvidenceType,
    LearningTrajectory,
    Misconception,
    MisconceptionStatus,
    Understanding,
    UnderstandingRevision,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace

T0 = datetime(2026, 10, 26, 23, 30)
QUESTION = "我之前对 TLB 的理解错在哪里？后来是怎么纠正的？"


def _understanding() -> Understanding:
    return Understanding(
        id=500,
        concept_id=10,
        concept_name="TLB",
        summary="已理解：TLB 缓存的是页表项。",
        known_parts=["TLB 缓存的是页表项，不是整个页表"],
        misconceptions=[],
        status=UnderstandingStatus.DEMONSTRATED,
    )


def _evidence(
    evidence_id: int,
    observation: str,
    correctness: EvidenceCorrectness,
    *,
    evidence_type: EvidenceType = EvidenceType.USER_EXPLANATION,
    source_type: str = "MESSAGE",
    minutes: int = 0,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        concept_id=10,
        evidence_type=evidence_type,
        observation=observation,
        correctness=correctness,
        created_at=T0 + timedelta(minutes=minutes),
        sources=[EvidenceSourceItem(source_type=source_type, source_id=evidence_id)],
    )


def _default_evidences() -> list[EvidenceItem]:
    return [
        _evidence(
            101,
            "我理解 TLB 就是页表本身，它缓存的是整个页表",
            EvidenceCorrectness.PARTIAL,
        ),
        _evidence(
            102,
            "TLB 就是页表本身，它缓存的是整个页表",
            EvidenceCorrectness.INCORRECT,
            evidence_type=EvidenceType.USER_CORRECTION,
            source_type="HIGHLIGHT",
            minutes=1,
        ),
        _evidence(
            103,
            "TLB 缓存的是页表项",
            EvidenceCorrectness.CORRECT,
            evidence_type=EvidenceType.USER_CORRECTION,
            source_type="HIGHLIGHT",
            minutes=2,
        ),
        _evidence(
            104,
            "TLB 缓存的是页表项，不是整个页表",
            EvidenceCorrectness.CORRECT,
            evidence_type=EvidenceType.TEST_RESULT,
            source_type="TEST_ANSWER",
            minutes=3,
        ),
    ]


def _revisions() -> list[UnderstandingRevision]:
    return [
        UnderstandingRevision(
            id=1002,
            concept_id=10,
            revision_no=2,
            misconceptions=[
                Misconception(text="TLB 就是页表本身，它缓存的是整个页表", status=MisconceptionStatus.ACTIVE)
            ],
            status=UnderstandingStatus.MISCONCEPTION,
            trigger_evidence_id=102,
            created_at=T0 + timedelta(minutes=1),
        ),
        UnderstandingRevision(
            id=1004,
            concept_id=10,
            revision_no=4,
            status=UnderstandingStatus.DEMONSTRATED,
            trigger_evidence_id=104,
            created_at=T0 + timedelta(minutes=3),
        ),
    ]


def _state(*, with_trajectory: bool, evidences: list[EvidenceItem] | None = None) -> AgentState:
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message=QUESTION,
        understandings=[_understanding()],
        history_review_requested=True,
        trace=RunTrace(run_id=1),
    )
    if with_trajectory:
        state.trajectories = [
            LearningTrajectory(
                concept_id=10,
                concept_name="TLB",
                current_status=UnderstandingStatus.DEMONSTRATED,
                revisions=_revisions(),
                evidences=evidences if evidences is not None else _default_evidences(),
            )
        ]
    return state


def _kinds(issues) -> set[str]:
    return {issue.kind for issue in issues}


def test_unrecorded_internal_process_is_flagged() -> None:
    issues = check_history_answer(
        "你很可能通过重读定义 + 对比缓存通用含义迁移，才形成了正确理解。",
        _state(with_trajectory=True),
    )

    assert "internal_process" in _kinds(issues)


def test_external_help_absence_is_flagged() -> None:
    issues = check_history_answer(
        "你后来答对了，而且没有外部提示或资料介入。",
        _state(with_trajectory=True),
    )

    assert "external_absence" in _kinds(issues)


def test_recorded_process_term_does_not_excuse_another_fabricated_process() -> None:
    evidences = [
        _evidence(
            202,
            "我当时意识到 TLB 缓存的是页表项",
            EvidenceCorrectness.CORRECT,
        )
    ]
    issues = check_history_answer(
        "你意识到 TLB 缓存的是页表项，而且你自己推导出了整个地址翻译流程。",
        _state(with_trajectory=True, evidences=evidences),
    )

    internal = [issue for issue in issues if issue.kind == "internal_process"]
    assert internal
    assert any("自己推导" in issue.reason for issue in internal)


def test_uncertainty_note_does_not_hide_a_strong_absence_claim() -> None:
    issues = check_history_answer(
        "记录里没有写你是否借助资料，你完全靠自己。",
        _state(with_trajectory=True),
    )

    assert "external_absence" in _kinds(issues)


def test_internal_process_that_is_quoted_from_the_record_is_allowed() -> None:
    evidences = [
        _evidence(
            201,
            "我当时意识到 TLB 缓存的是页表项，不是整个页表",
            EvidenceCorrectness.CORRECT,
            evidence_type=EvidenceType.USER_EXPLANATION,
        )
    ]
    issues = check_history_answer(
        "记录里写着你当时意识到 TLB 缓存的是页表项。",
        _state(with_trajectory=True, evidences=evidences),
    )

    assert not _kinds(issues) & {"internal_process"}


def test_partial_cannot_be_upgraded_to_wrong_or_completely_correct() -> None:
    evidences = [
        _evidence(301, "TLB 是页表缓存", EvidenceCorrectness.PARTIAL),
    ]
    issues = check_history_answer(
        "你当时答错了，后来完全答对了。",
        _state(with_trajectory=True, evidences=evidences),
    )

    assert "partial_upgrade" in _kinds(issues)


def test_single_evidence_claim_is_checked_against_the_actual_count() -> None:
    issues = check_history_answer(
        "记录里唯一的证据就是这条批注。",
        _state(with_trajectory=True),
    )

    assert "record_completeness" in _kinds(issues)


def test_explicit_annotation_must_be_mapped_when_it_exists() -> None:
    issues = check_history_answer(
        "你一开始把 TLB 当成整个页表，后来改成了页表项。",
        _state(with_trajectory=True),
    )

    assert "missing_source_mapping" in _kinds(issues)


def test_no_history_denial_and_back_inference_are_flagged() -> None:
    issues = check_history_answer(
        "你以前没有这个错误。你之前应该是通过重读定义才理解的。",
        _state(with_trajectory=False),
    )

    kinds = _kinds(issues)
    assert "no_history_denial" in kinds
    assert "no_history_inference" in kinds


def test_no_history_cannot_infer_past_from_current_success() -> None:
    issues = check_history_answer(
        "你现在能讲清楚，说明你已经越过了一个常见误区，也避开了另一个混淆。"
        "当前可见的记录里没有你过去的错误表述。",
        _state(with_trajectory=False),
    )

    assert "no_history_inference" in _kinds(issues)


def test_no_history_acknowledgement_is_allowed() -> None:
    issues = check_history_answer(
        "当前可见的记录里没有你过去的错误表述、批注、测试回答或历史版本，"
        "所以我无法确认你过去是否出现过这个误解，也不能断言你从来没有出现过。"
        "当前状态是已能讲清。",
        _state(with_trajectory=False),
    )

    assert issues == []


def test_retry_instruction_only_carries_issue_categories() -> None:
    instruction = retry_instruction(
        [
            check_history_answer(
                "你很可能通过重读定义迁移了过来，而且没有外部提示。",
                _state(with_trajectory=True),
            )[0]
        ]
    )

    assert "上一次回答包含当前记录无法支撑的断言" in instruction
    assert "心理过程" in instruction or "外部提示" in instruction


def test_trajectory_fallback_is_built_from_records_and_has_no_internal_ids() -> None:
    fallback = build_history_fallback(_state(with_trajectory=True))

    assert "TLB 就是页表本身，它缓存的是整个页表" in fallback
    assert "TLB 缓存的是页表项" in fallback
    assert "后续正确记录：2 条" in fallback
    assert "记录没有写你为什么改变看法" in fallback
    assert "#101" not in fallback
    assert "批注#29" not in fallback


def test_no_history_fallback_refuses_to_deny_the_past() -> None:
    fallback = build_history_fallback(_state(with_trajectory=False))

    assert "没有关于TLB的过往错误表述" in fallback
    assert "无法确认你过去是否出现过这个误解" in fallback
    assert "不能断言你过去没有出现过" in fallback
    assert "你以前没有这个错误" not in fallback
