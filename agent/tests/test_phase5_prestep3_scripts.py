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
