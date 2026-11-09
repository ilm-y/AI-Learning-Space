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
    partial_upgrade_negation_skips,
    retry_instruction,
)
from app.schemas import (
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceSourceItem,
    EvidenceType,
    HistoryMessage,
    LearningTrajectory,
    MessageRole,
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


def _partial_only_state() -> AgentState:
    evidences = [
        _evidence(301, "TLB 是页表缓存", EvidenceCorrectness.PARTIAL),
    ]
    return _state(with_trajectory=True, evidences=evidences)


def test_negated_partial_upgrade_phrases_are_not_flagged() -> None:
    """KI-2：否定 / 对比语境里的「答错」「完全答对」不是升级断言。"""
    answers = [
        "不是完全答错。",
        "这是部分正确，而不是完全正确。",
        "记录并没有被写成『答错』。",
        "既不是完全答对也不是完全答错。",
        "这条标注只说明当时部分正确，并不表示全部答对。",
        "部分正确并不等于答错。",
    ]
    for answer in answers:
        issues = check_history_answer(answer, _partial_only_state())
        assert "partial_upgrade" not in _kinds(issues), (answer, issues)


def test_positive_partial_upgrade_phrases_still_flagged() -> None:
    """否定识别不能把肯定判断一起放行。"""
    answers = ["这是完全答对。", "这是答错。"]
    for answer in answers:
        issues = check_history_answer(answer, _partial_only_state())
        assert "partial_upgrade" in _kinds(issues), (answer, issues)


def test_contrast_after_negation_is_still_positive() -> None:
    """「不是 A，而是 B」：A 跳过，B 仍按肯定判断。"""
    issues = check_history_answer(
        "这不是答错，而是完全答对。", _partial_only_state()
    )
    assert "partial_upgrade" in _kinds(issues)


def test_negation_skip_observability_counts_literal_hits() -> None:
    assert partial_upgrade_negation_skips("不是完全答错") == ["答错"]
    assert partial_upgrade_negation_skips("既不是完全答对也不是完全答错") == [
        "完全答对",
        "答错",
    ]
    assert partial_upgrade_negation_skips("这是答错") == []


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


QUOTE_QUESTION = "回顾一下，我之前对 TCP 慢启动的理解错在哪？请引用我当时的原话。"
HISTORY_ORIGINAL = "我原来以为慢启动慢是因为它每次只把窗口加一"


def _quote_state() -> AgentState:
    state = _state(with_trajectory=False)
    state.user_message = QUOTE_QUESTION
    state.history = [
        HistoryMessage(
            role=MessageRole.USER,
            content=f"【S5-HISTORY】{HISTORY_ORIGINAL}。",
        )
    ]
    return state


def test_fallback_preserves_quote_the_user_explicitly_requested() -> None:
    first = (
        "你当时的原话是：「我原来以为慢启动慢是因为它每次只把窗口加一。」"
        "你是完全靠自己纠正过来的。"
    )
    fallback = build_history_fallback(_quote_state(), first_answer=first)

    assert HISTORY_ORIGINAL in fallback
    assert "「我原来以为慢启动慢是因为它每次只把窗口加一。」" in fallback
    # 同句里的无依据断言不随引用一起回退。
    assert "完全靠自己" not in fallback
    # 确定性回退正文仍然保留。
    assert "不能断言你过去没有出现过" in fallback


def test_fallback_does_not_preserve_quote_without_explicit_request() -> None:
    first = f"你当时的原话是：「{HISTORY_ORIGINAL}。」"
    state = _quote_state()
    state.user_message = "我之前对 TLB 的理解错在哪里？"

    fallback = build_history_fallback(state, first_answer=first)

    assert HISTORY_ORIGINAL not in fallback


def test_fallback_does_not_preserve_an_unrecorded_sentence() -> None:
    first = "你当时说：「这是一句记录里没有、临时编出来的句子。」"
    fallback = build_history_fallback(_quote_state(), first_answer=first)

    assert "临时编出来的句子" not in fallback


def _plain_evidence_state() -> AgentState:
    """无显式批注来源的状态：本组只验证不确定性措辞，不混入 source mapping 规则。"""
    evidences = [
        _evidence(401, "TLB 缓存的是页表项", EvidenceCorrectness.CORRECT),
    ]
    return _state(with_trajectory=True, evidences=evidences)


def test_compliant_uncertainty_disclaimers_are_not_flagged() -> None:
    """A01-r2 回归：说明「记录没写」不是「没有发生」。"""
    answers = [
        "那次纠正是独立完成的还是有人指出——同样没有记录。",
        "你当时的心情、感受，以及是否意识到问题的根源——这些都没有记录。",
        (
            "你当时为什么会那样理解、后来是怎么改过来的、有没有借助外部资料，"
            "这些在记录里都没有，我不做推测。"
        ),
        "记录里没有写你是否借助外部资料或他人提示，因此这一点无法确认。",
        "记录里没有说明你当时有没有意识到这个误区。",
        # 以下三句逐字来自 A01-r2 的 historyGuard.excerpts。
        "**你当时为什么会那样理解、后来是怎么改过来的、有没有借助外部资料**，这些在记录里都没有，我不做推测。",
        "- 那次纠正是独立完成的还是有人指出——同样没有记录。",
        "- 你当时的心情、感受，以及是否意识到问题的根源——这些都没有记录。",
    ]
    for answer in answers:
        issues = check_history_answer(answer, _plain_evidence_state())
        assert not issues, (answer, [issue.as_dict() for issue in issues])


def test_uncertainty_note_does_not_excuse_a_fabricated_assertion() -> None:
    """说明不足以掩护另一从句里的肯定断言。"""
    cases = [
        ("记录里没有写过程，但你突然想通了。", "internal_process"),
        ("记录里没有写外部帮助，你完全靠自己。", "external_absence"),
        ("你当时没有借助外部资料，虽然记录没有写。", "external_absence"),
        ("你当时自己推导出了正确关系。", "internal_process"),
    ]
    for answer, expected_kind in cases:
        issues = check_history_answer(answer, _plain_evidence_state())
        assert expected_kind in _kinds(issues), (answer, issues)


def test_compliant_disclaimer_does_not_trigger_deterministic_fallback() -> None:
    state = _plain_evidence_state()
    answer = (
        "记录里可以看出你之前把 TLB 当成整个页表，后来通过测试回答纠正为页表项。"
        "至于那次纠正是独立完成的还是有人指出——同样没有记录，我不做推测。"
    )
    assert check_history_answer(answer, state) == []
    assert "我只能按当前记录复述" not in answer
