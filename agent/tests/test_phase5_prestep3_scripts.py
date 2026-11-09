"""Phase 5 pre-Step3 脚本的纯函数回归（不连 MySQL / Qdrant）。

覆盖批次 3 四项决策中可离线验证的部分：

1. ``restore_snapshot`` 的删除范围必须同时满足「同一 conversation +
   snapshotTime 之后 + 非快照历史」；
2. ``qdrant_freeze_check`` 的 payload 规范化、point 哈希与基线差异检测；
3. 配置前缀严格相等由 ``test_context_strategy.py`` 覆盖，这里不重复。
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "phase5" / "pre-step3"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import qdrant_freeze_check as qdrant  # noqa: E402
import restore_snapshot as restore  # noqa: E402
import run_c_verification as run_c  # noqa: E402
from snapshot_common import FIXTURE_MARKER, REVIEW_QUESTION  # noqa: E402

from app.context.policy import Intent, RetrievalNeed, plan_retrieval  # noqa: E402


def _snapshot() -> dict:
    return {
        "kind": "phase5-pre-step3-snapshot",
        "snapshotTime": "2026-11-09 12:00:00",
        "dataset": {
            "conversationId": 55,
            "historyMessageIds": [66, 67],
        },
    }


def test_prune_scope_is_exactly_conversation_and_time_and_history() -> None:
    snapshot = _snapshot()
    scope = restore.prune_scope(snapshot)
    sql, params = restore.prune_sql(scope, delete=True)

    assert sql.startswith("DELETE FROM message")
    assert "conversation_id=%s" in sql
    assert "created_at > %s" in sql
    assert "id NOT IN (%s,%s)" in sql
    assert params == (55, "2026-11-09 12:00:00", 66, 67)


def test_prune_scope_rejects_messages_outside_all_three_conditions() -> None:
    scope = restore.prune_scope(_snapshot())

    assert restore.is_prunable_message(
        {"id": 200, "conversation_id": 55, "created_at": "2026-11-09 12:00:01"},
        scope,
    )
    # 快照历史消息 ID
    assert not restore.is_prunable_message(
        {"id": 66, "conversation_id": 55, "created_at": "2026-11-09 12:00:01"},
        scope,
    )
    # 其他 conversation
    assert not restore.is_prunable_message(
        {"id": 201, "conversation_id": 56, "created_at": "2026-11-09 12:00:01"},
        scope,
    )
    # snapshotTime 之前
    assert not restore.is_prunable_message(
        {"id": 202, "conversation_id": 55, "created_at": "2026-11-09 11:59:59"},
        scope,
    )
    # 恰好等于 snapshotTime 也不属于「之后」
    assert not restore.is_prunable_message(
        {"id": 203, "conversation_id": 55, "created_at": "2026-11-09 12:00:00"},
        scope,
    )


def test_qdrant_payload_hash_is_stable_and_id_bound() -> None:
    payload_a = {"document_id": 1, "text": "TCP 三次握手"}
    payload_b = {"text": "TCP 三次握手", "document_id": 1}

    assert qdrant.canonical_payload(payload_a) == qdrant.canonical_payload(payload_b)
    assert qdrant.point_hash(1, payload_a) == qdrant.point_hash(1, payload_b)
    assert qdrant.point_hash(1, payload_a) != qdrant.point_hash(2, payload_a)
    assert qdrant.point_hash(1, payload_a) != qdrant.point_hash(
        1, {**payload_a, "text": "不同"}
    )


def test_qdrant_combined_hash_is_order_independent() -> None:
    a = {"1": qdrant.point_hash(1, {"x": 1}), "2": qdrant.point_hash(2, {"x": 2})}
    b = {"2": qdrant.point_hash(2, {"x": 2}), "1": qdrant.point_hash(1, {"x": 1})}

    assert qdrant.combined_hash(a) == qdrant.combined_hash(b)


def test_qdrant_diff_detects_count_id_and_payload_changes() -> None:
    payload = {"document_id": 1, "text": "TCP"}
    baseline = qdrant.build_state("c", {"1": qdrant.point_hash(1, payload)}, 1)

    same = qdrant.build_state("c", {"1": qdrant.point_hash(1, payload)}, 1)
    assert qdrant.diff_state(baseline, same) == []

    assert qdrant.diff_state(
        baseline, qdrant.build_state("c", {}, 0)
    )  # 缺失 point
    assert qdrant.diff_state(
        baseline,
        qdrant.build_state(
            "c", {"1": qdrant.point_hash(1, payload), "2": qdrant.point_hash(2, payload)}, 2
        ),
    )  # 新增 point
    assert qdrant.diff_state(
        baseline,
        qdrant.build_state(
            "c", {"1": qdrant.point_hash(1, {**payload, "text": "变了"})}, 1
        ),
    )  # payload 变化
    assert qdrant.diff_state(
        baseline, qdrant.build_state("other", {"1": qdrant.point_hash(1, payload)}, 1)
    )  # collection 变化


def test_qdrant_script_has_no_write_api_calls() -> None:
    source = (SCRIPT_DIR / "qdrant_freeze_check.py").read_text(encoding="utf-8")

    for forbidden in (
        "/points/" + "delete",
        "/points/" + "upsert",
        "requests." + "put(",
        "requests." + "delete(",
    ):
        assert forbidden not in source


# ---------- 批次 3 修正：问题意图与 Manifest token 守恒 ----------


def test_review_question_is_classified_as_learning_review() -> None:
    """修正后的判据只有在问题真的进入复盘 intent 时才成立。"""
    plan = plan_retrieval(
        user_message=REVIEW_QUESTION,
        concept_ids=[1],
        session=None,
        space_concepts=[],
    )

    assert plan.intent is Intent.LEARNING_REVIEW
    assert plan.review_required is True
    assert plan.revision.need is RetrievalNeed.REQUIRED
    assert plan.memory.need is RetrievalNeed.REQUIRED
    # 「回顾」在 EVIDENCE_TRIGGERS 中，复盘问题同时要求 evidence。
    assert plan.evidence.need is RetrievalNeed.REQUIRED


def test_review_question_explicitly_asks_for_the_fixture_marker() -> None:
    """逐字标记判据必须由问题显式要求，不能依赖模型自觉。"""
    assert "方括号标记" in REVIEW_QUESTION
    assert FIXTURE_MARKER not in REVIEW_QUESTION  # 不直接把答案写进问题


def _review_manifest(per_type: dict) -> dict:
    return {
        "intent": "LEARNING_REVIEW",
        "sources": [
            {
                "type": "trajectory",
                "source_id": "revision:88",
                "retrieval_status": "SUCCESS",
                "injection_status": "SELECTED",
                "reason": "in_budget",
                "estimated_tokens": 10,
            },
            {
                "type": "trajectory_source",
                "source_id": "evidence:99",
                "retrieval_status": "SUCCESS",
                "injection_status": "SELECTED",
                "reason": "in_budget",
                "estimated_tokens": 10,
            },
        ],
        "budget": {"per_type": per_type},
    }


def test_manifest_token_conservation_aligns_exact_type() -> None:
    """trajectory 与 trajectory_source 分别对各自的 per_type key。

    这是 run 1 复盘 Manifest 的真实形状：按基类型分组合把两份 entry 相加，
    却只与 per_type["trajectory"] 比较，产生假失败。
    """
    snapshot = {"dataset": {"revisionId": 88, "evidenceId": 99}}
    manifest = _review_manifest(
        {
            "trajectory": {"used": 10},
            "trajectory_source": {"used": 10},
        }
    )
    check = run_c.validate_manifest(manifest, snapshot)

    assert check["problems"] == []
    assert check["intent"] == "LEARNING_REVIEW"
    assert check["revisionEntry"] is True
    assert check["evidenceEntry"] is True


def test_manifest_token_conservation_detects_real_mismatch() -> None:
    snapshot = {"dataset": {"revisionId": 88, "evidenceId": 99}}
    manifest = _review_manifest(
        {
            "trajectory": {"used": 10},
            "trajectory_source": {"used": 5},  # 实际 entry 是 10
        }
    )

    problems = run_c.validate_manifest(manifest, snapshot)["problems"]
    assert any("trajectory_source token 拆分不守恒" in item for item in problems)


def test_manifest_requires_revision_entry_for_learning_review() -> None:
    snapshot = {"dataset": {"revisionId": 88, "evidenceId": 99}}
    manifest = _review_manifest(
        {
            "trajectory": {"used": 10},
            "trajectory_source": {"used": 10},
        }
    )
    manifest["sources"] = [manifest["sources"][1]]  # 只剩 evidence

    problems = run_c.validate_manifest(manifest, snapshot)["problems"]
    assert any("revision entry" in item for item in problems)
