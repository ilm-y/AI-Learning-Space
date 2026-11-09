"""把隔离库恢复到统一快照的初始状态。

为什么需要：三组必须读同一份输入。每组运行会向同一个 conversation 追加
USER/ASSISTANT 消息（Spring 负责落库），所以下一组之前必须把这段对话与
个人上下文恢复成快照初始值。

恢复方式：

- 只对快照清单里记录的行做 ``INSERT ... ON DUPLICATE KEY UPDATE``（显式 ID），
  内容与时间戳全部来自 ``snapshotTime``，因此恢复后的冻结表哈希与基线一致；
- 默认**不删除任何行**；运行中追加的消息会在报告里列出数量；
- 只有显式 ``--prune-run-messages`` 才会删除「同一 conversation 内、ID 不在
  快照历史消息列表中」的消息——它们按定义是三组实验自己写出来的。

前置：分析消费者（Understanding / Evidence / Memory 写回）必须关闭。本脚本
不会删除它没有创建的行；如果分析写回开着，漂移只能人工处理。

不打印、不写盘任何密钥；默认 dry-run，只有 ``--apply`` 才写库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from snapshot_common import (
    EVIDENCE_OBSERVATION,
    HISTORY_ASSISTANT,
    HISTORY_USER,
    MEMORY_CONTENT,
    PASSWORD,  # noqa: F401 - 仅用于说明快照账号口令来源，不输出
    SESSION_FOCUS,
    SESSION_GOAL,
    UNDERSTANDING_EXPLANATION,
    UNDERSTANDING_KNOWN,
    UNDERSTANDING_SUMMARY,
    UNDERSTANDING_UNCERTAIN,
    assert_isolated_state,
    connect_mysql,
    write_json,
)

CONCEPT_DESCRIPTION = "TCP 建立连接的三次报文交换"
SESSION_TITLE = "TCP 三次握手：第三次 ACK"
ANALYZER_VERSION = "phase5-prestep3-fixture"

Statement = tuple[str, tuple[Any, ...]]


def load_snapshot(path: Path) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("kind") != "phase5-pre-step3-snapshot":
        raise SystemExit(f"不是 pre-Step3 快照清单：{path}")
    assert_isolated_state(snapshot["isolation"])
    return snapshot


def dedupe_key(user_id: int, concept_id: int, message_id: int) -> str:
    return hashlib.sha256(
        (
            f"{user_id}:{concept_id}:USER_EXPLANATION:{message_id}:{EVIDENCE_OBSERVATION}"
        ).encode("utf-8")
    ).hexdigest()


def build_statements(snapshot: dict[str, Any]) -> list[Statement]:
    """按依赖顺序生成恢复语句；全部是 upsert，不删除数据。"""
    data = snapshot["dataset"]
    now = snapshot["snapshotTime"]
    user_id = int(data["userId"])
    space_id = int(data["spaceId"])
    concept_id = int(data["conceptId"])
    session_id = int(data["sessionId"])
    conversation_id = int(data["conversationId"])
    message_ids = [int(value) for value in data["historyMessageIds"]]
    understanding_id = int(data["understandingId"])
    revision_id = int(data["revisionId"])
    evidence_id = int(data["evidenceId"])
    evidence_source_id = int(data["evidenceSourceId"])
    memory_id = int(data["memoryId"])

    known_parts = json.dumps(UNDERSTANDING_KNOWN, ensure_ascii=False)
    misconceptions = json.dumps([], ensure_ascii=False)
    uncertain_parts = json.dumps(UNDERSTANDING_UNCERTAIN, ensure_ascii=False)
    space_name = data.get("spaceName") or f"Phase 5 pre-Step3 {snapshot['env']['stamp']}"

    statements: list[Statement] = [
        (
            "INSERT INTO learning_space(id,user_id,name,description,visibility,deleted_at) "
            "VALUES(%s,%s,%s,%s,'PRIVATE',NULL) ON DUPLICATE KEY UPDATE "
            "name=VALUES(name),description=VALUES(description),visibility='PRIVATE',"
            "deleted_at=NULL",
            (space_id, user_id, space_name, "Phase 5 pre-Step3 A/B/C 统一快照：TCP 三次握手"),
        ),
        (
            "INSERT INTO concept(id,learning_space_id,canonical_name,display_name,"
            "description,difficulty_level,deleted_at) "
            "VALUES(%s,%s,'TCP 三次握手','TCP 三次握手',%s,2,NULL) "
            "ON DUPLICATE KEY UPDATE canonical_name='TCP 三次握手',"
            "display_name='TCP 三次握手',description=VALUES(description),"
            "difficulty_level=2,deleted_at=NULL",
            (concept_id, space_id, CONCEPT_DESCRIPTION),
        ),
        (
            "INSERT INTO learning_session(id,user_id,learning_space_id,title,goal,"
            "current_focus,status,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'ACTIVE',NULL) ON DUPLICATE KEY UPDATE "
            "title=VALUES(title),goal=VALUES(goal),current_focus=VALUES(current_focus),"
            "status='ACTIVE',completed_at=NULL",
            (session_id, user_id, space_id, SESSION_TITLE, SESSION_GOAL, SESSION_FOCUS),
        ),
        (
            "INSERT INTO conversation(id,user_id,learning_space_id,session_id,title,"
            "status,last_message_seq,last_message_at,deleted_at) "
            "VALUES(%s,%s,%s,%s,%s,'ACTIVE',%s,%s,NULL) ON DUPLICATE KEY UPDATE "
            "session_id=VALUES(session_id),title=VALUES(title),status='ACTIVE',"
            "last_message_seq=VALUES(last_message_seq),"
            "last_message_at=VALUES(last_message_at),deleted_at=NULL",
            (
                conversation_id,
                user_id,
                space_id,
                session_id,
                "pre-Step3 统一快照对话",
                2,
                now,
            ),
        ),
    ]

    for sequence, (message_id, role, text) in enumerate(
        zip(message_ids, ("USER", "ASSISTANT"), (HISTORY_USER, HISTORY_ASSISTANT)),
        start=1,
    ):
        statements.append(
            (
                "INSERT INTO message(id,conversation_id,role,message_type,content,tokens,"
                "truncated,sequence_no,deleted_at,created_at) "
                "VALUES(%s,%s,%s,'TEXT',%s,NULL,0,%s,NULL,%s) ON DUPLICATE KEY UPDATE "
                "conversation_id=VALUES(conversation_id),role=VALUES(role),"
                "message_type='TEXT',content=VALUES(content),tokens=NULL,truncated=0,"
                "sequence_no=VALUES(sequence_no),deleted_at=NULL,created_at=VALUES(created_at)",
                (message_id, conversation_id, role, text, sequence, now),
            )
        )

    statements.extend(
        [
            (
                "INSERT INTO learning_understanding(id,user_id,concept_id,summary,"
                "known_parts,misconceptions,uncertain_parts,latest_user_explanation,"
                "status,current_revision_id,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'PARTIAL',%s,%s) "
                "ON DUPLICATE KEY UPDATE user_id=VALUES(user_id),"
                "concept_id=VALUES(concept_id),summary=VALUES(summary),"
                "known_parts=VALUES(known_parts),misconceptions=VALUES(misconceptions),"
                "uncertain_parts=VALUES(uncertain_parts),"
                "latest_user_explanation=VALUES(latest_user_explanation),status='PARTIAL',"
                "current_revision_id=VALUES(current_revision_id),updated_at=VALUES(updated_at)",
                (
                    understanding_id,
                    user_id,
                    concept_id,
                    UNDERSTANDING_SUMMARY,
                    known_parts,
                    misconceptions,
                    uncertain_parts,
                    UNDERSTANDING_EXPLANATION,
                    revision_id,
                    now,
                ),
            ),
            (
                "INSERT INTO learning_evidence(id,user_id,concept_id,understanding_id,"
                "evidence_type,observation,correctness,reasoning_quality,specificity,"
                "ai_confidence,need_follow_up,follow_up_status,follow_up_conversation_id,"
                "dedupe_key,first_seen_run_id,last_seen_run_id,analyzer_version,"
                "created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,'USER_EXPLANATION',%s,'PARTIAL',NULL,NULL,NULL,1,"
                "'PENDING',NULL,%s,NULL,NULL,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "user_id=VALUES(user_id),concept_id=VALUES(concept_id),"
                "understanding_id=VALUES(understanding_id),evidence_type='USER_EXPLANATION',"
                "observation=VALUES(observation),correctness='PARTIAL',"
                "reasoning_quality=NULL,specificity=NULL,ai_confidence=NULL,"
                "need_follow_up=1,follow_up_status='PENDING',"
                "follow_up_conversation_id=NULL,dedupe_key=VALUES(dedupe_key),"
                "first_seen_run_id=NULL,last_seen_run_id=NULL,"
                "analyzer_version=VALUES(analyzer_version),"
                "created_at=VALUES(created_at),updated_at=VALUES(updated_at)",
                (
                    evidence_id,
                    user_id,
                    concept_id,
                    understanding_id,
                    EVIDENCE_OBSERVATION,
                    dedupe_key(user_id, concept_id, message_ids[0]),
                    ANALYZER_VERSION,
                    now,
                    now,
                ),
            ),
            (
                "INSERT INTO evidence_source(id,evidence_id,source_type,source_id,created_at) "
                "VALUES(%s,%s,'MESSAGE',%s,%s) ON DUPLICATE KEY UPDATE "
                "evidence_id=VALUES(evidence_id),source_type='MESSAGE',"
                "source_id=VALUES(source_id),created_at=VALUES(created_at)",
                (evidence_source_id, evidence_id, message_ids[0], now),
            ),
            (
                "INSERT INTO understanding_revision(id,understanding_id,revision_no,summary,"
                "known_parts,misconceptions,uncertain_parts,user_explanation,status,"
                "trigger_evidence_id,created_at) "
                "VALUES(%s,%s,1,%s,%s,%s,%s,%s,'PARTIAL',%s,%s) ON DUPLICATE KEY UPDATE "
                "understanding_id=VALUES(understanding_id),revision_no=1,"
                "summary=VALUES(summary),known_parts=VALUES(known_parts),"
                "misconceptions=VALUES(misconceptions),"
                "uncertain_parts=VALUES(uncertain_parts),"
                "user_explanation=VALUES(user_explanation),status='PARTIAL',"
                "trigger_evidence_id=VALUES(trigger_evidence_id),created_at=VALUES(created_at)",
                (
                    revision_id,
                    understanding_id,
                    UNDERSTANDING_SUMMARY,
                    known_parts,
                    misconceptions,
                    uncertain_parts,
                    UNDERSTANDING_EXPLANATION,
                    evidence_id,
                    now,
                ),
            ),
            (
                "INSERT INTO memory(id,user_id,scope_type,scope_key,learning_space_id,"
                "concept_id,memory_type,memory_key,content,status,importance,version,"
                "embedding_ref,created_at,updated_at) "
                "VALUES(%s,%s,'CONCEPT',%s,%s,%s,'LEARNING','phase5-prestep3-tcp-style',"
                "%s,'ACTIVE',0.9,0,NULL,%s,%s) ON DUPLICATE KEY UPDATE "
                "user_id=VALUES(user_id),scope_type='CONCEPT',scope_key=VALUES(scope_key),"
                "learning_space_id=VALUES(learning_space_id),concept_id=VALUES(concept_id),"
                "memory_type='LEARNING',memory_key=VALUES(memory_key),"
                "content=VALUES(content),status='ACTIVE',importance=0.9,version=0,"
                "embedding_ref=NULL,created_at=VALUES(created_at),updated_at=VALUES(updated_at)",
                (
                    memory_id,
                    user_id,
                    f"CONCEPT:{concept_id}",
                    space_id,
                    concept_id,
                    MEMORY_CONTENT,
                    now,
                    now,
                ),
            ),
        ]
    )
    return statements


def extra_messages(connection, snapshot: dict[str, Any]) -> list[int]:
    data = snapshot["dataset"]
    message_ids = [int(value) for value in data["historyMessageIds"]]
    placeholders = ",".join(["%s"] * len(message_ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM message WHERE conversation_id=%s AND id NOT IN ({placeholders}) "
            "ORDER BY id",
            (int(data["conversationId"]), *message_ids),
        )
        return [int(row["id"]) for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?", help="setup_snapshot.py 输出的快照清单")
    parser.add_argument("--apply", action="store_true", help="真正执行恢复")
    parser.add_argument(
        "--prune-run-messages",
        action="store_true",
        help="删除同一 conversation 内不属于快照历史的消息（三组实验自己写出的）",
    )
    parser.add_argument("--json-out", help="把结果写入 JSON 文件")
    parser.add_argument("--self-test", action="store_true", help="不连库自检")
    args = parser.parse_args()

    if args.self_test:
        result = self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["problems"] else 1
    if not args.snapshot:
        parser.error("需要快照清单路径（或使用 --self-test）")

    snapshot = load_snapshot(Path(args.snapshot))
    statements = build_statements(snapshot)

    print(f"恢复语句 {len(statements)} 条；prune-run-messages={args.prune_run_messages}")
    for index, (sql, params) in enumerate(statements, start=1):
        print(f"{index:02d}. {sql.split(' ON DUPLICATE KEY UPDATE')[0].strip()}")

    if not args.apply:
        print("\n[dry-run] 未写库。确认后用 --apply 执行。")
        return 0

    connection = connect_mysql(snapshot["isolation"]["db"])
    try:
        extras = extra_messages(connection, snapshot)
        print(f"\n快照对话中运行期追加的消息：{len(extras)} 条")
        if extras and not args.prune_run_messages:
            print("WARN: 未加 --prune-run-messages，这些消息会保留，下一组历史将与快照不同。")

        connection.begin()
        with connection.cursor() as cursor:
            for sql, params in statements:
                cursor.execute(sql, params)
            if args.prune_run_messages and extras:
                placeholders = ",".join(["%s"] * len(extras))
                cursor.execute(
                    f"DELETE FROM message WHERE id IN ({placeholders})", tuple(extras)
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    result = {
        "action": "apply",
        "statements": len(statements),
        "prunedMessages": len(extras) if args.prune_run_messages else 0,
        "keptRunMessages": 0 if args.prune_run_messages else len(extras),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("下一步：python freeze_check.py <snapshot> --check")
    if args.json_out:
        write_json(Path(args.json_out), result)
    return 0


def _fake_snapshot() -> dict[str, Any]:
    return {
        "kind": "phase5-pre-step3-snapshot",
        "fixtureMarker": "PHASE5-PRESTEP3-SNAPSHOT",
        "snapshotTime": "2026-11-09 12:00:00",
        "env": {"stamp": "20261109120000-abcdef"},
        "isolation": {
            "db": "phase5_pilot_20261109120000_abcdef",
            "collectionPrefix": "phase5-pilot-20261109120000-abcdef",
            "memoryCollectionPrefix": "phase5-pilot-20261109120000-abcdef-memory",
        },
        "dataset": {
            "userId": 11,
            "username": "prestep3_test",
            "spaceId": 22,
            "spaceName": "Phase 5 pre-Step3 20261109120000-abcdef",
            "conceptId": 33,
            "sessionId": 44,
            "conversationId": 55,
            "historyMessageIds": [66, 67],
            "understandingId": 77,
            "revisionId": 88,
            "evidenceId": 99,
            "evidenceSourceId": 100,
            "memoryId": 111,
        },
    }


def self_test() -> dict[str, Any]:
    problems: list[str] = []
    snapshot = _fake_snapshot()
    statements = build_statements(snapshot)
    joined = "\n".join(sql for sql, _ in statements)

    if any(sql.lstrip().upper().startswith("DELETE") for sql, _ in statements):
        problems.append("build_statements 不应包含删除语句")
    if "ON DUPLICATE KEY UPDATE" not in joined:
        problems.append("恢复语句不是 upsert")
    if "last_message_seq=VALUES(last_message_seq)" not in joined:
        problems.append("未重置 conversation 消息游标")
    expected_tables = (
        "learning_space",
        "concept",
        "learning_session",
        "conversation",
        "message",
        "learning_understanding",
        "learning_evidence",
        "evidence_source",
        "understanding_revision",
        "memory",
    )
    for table in expected_tables:
        if f"INSERT INTO {table}" not in joined:
            problems.append(f"缺少 {table} 恢复语句")

    message_statements = [sql for sql, _ in statements if sql.startswith("INSERT INTO message")]
    if len(message_statements) != 2:
        problems.append(f"历史消息恢复条数应为 2，实际 {len(message_statements)}")

    if dedupe_key(11, 33, 66) != dedupe_key(11, 33, 66):
        problems.append("dedupe_key 不稳定")
    if dedupe_key(11, 33, 66) == dedupe_key(11, 33, 67):
        problems.append("dedupe_key 未绑定来源消息")

    for broken in (
        {**snapshot["isolation"], "db": "ails"},
        {**snapshot["isolation"], "collectionPrefix": "ails_documents"},
        {**snapshot["isolation"], "memoryCollectionPrefix": "ails_memories"},
    ):
        try:
            assert_isolated_state(broken)
        except SystemExit:
            pass
        else:
            problems.append(f"隔离断言漏放：{broken}")

    return {
        "statements": len(statements),
        "tables": list(expected_tables),
        "problems": problems,
    }


if __name__ == "__main__":
    sys.exit(main())
