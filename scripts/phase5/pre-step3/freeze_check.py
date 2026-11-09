"""冻结检查：证明三组 A/B/C 读的是同一份数据快照。

做三件事：

1. ``--write-baseline``：快照准备完成后立刻记录所有冻结表的行数与
   SHA-256（规范化 JSON 逐行哈希），以及生成参数指纹；
2. ``--check``：在任一组运行前后重新计算并比对；任一表或生成参数漂移即
   非零退出；
3. ``--dump``：只读打印当前哈希，便于人工排查。

本脚本只读 MySQL（``SET SESSION TRANSACTION READ ONLY``，从不 commit），
不调用 LLM / Embedding / Tavily，不写任何生产数据。

哈希范围刻意排除运行期可变字段（会话游标、last_active_at、索引重试与
对账状态等），避免把“运行痕迹”误报成“数据漂移”。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from snapshot_common import (
    FIXTURE_MARKER,
    TableSpec,
    assert_isolated_state,
    combined_hash,
    connect_mysql,
    freeze_tables,
    generation_profile,
    hash_rows,
    read_table,
    snapshot_hashes,
    write_json,
)

REQUIRED_PROFILE_KEYS = (
    "AILS_LLM_PROVIDER",
    "AILS_LLM_MODEL",
    "AILS_EMBEDDING_MODEL",
    "AILS_EMBEDDING_DIMENSION",
)


def load_snapshot(path: Path) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("kind") != "phase5-pre-step3-snapshot":
        raise SystemExit(f"不是 pre-Step3 快照清单：{path}")
    assert_isolated_state(snapshot["isolation"])
    return snapshot


def default_baseline_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(".baseline.json")


def dataset_fingerprint(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "fixtureMarker": snapshot.get("fixtureMarker"),
            "dataset": snapshot.get("dataset"),
            "question": snapshot.get("question"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    connection = connect_mysql(snapshot["isolation"]["db"])
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            except Exception:  # noqa: BLE001 - 旧版本 MySQL 不支持时退化为普通只读
                pass
        hashes = snapshot_hashes(connection, snapshot)
    finally:
        connection.rollback()
        connection.close()
    return {
        "tables": hashes,
        "combined": combined_hash(hashes),
        "generationProfile": generation_profile(),
    }


def write_baseline(snapshot_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    state = current_state(snapshot)
    baseline = {
        "schemaVersion": 1,
        "kind": "phase5-pre-step3-baseline",
        "snapshotPath": str(snapshot_path),
        "writtenAt": datetime.now().isoformat(timespec="seconds"),
        "fixtureMarker": snapshot.get("fixtureMarker"),
        "datasetFingerprint": dataset_fingerprint(snapshot),
        "tables": state["tables"],
        "combined": state["combined"],
        "generationProfile": state["generationProfile"],
    }
    path = default_baseline_path(snapshot_path)
    write_json(path, baseline)
    return baseline


def diff_baseline(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in sorted(set(baseline["tables"]) | set(current["tables"])):
        before = baseline["tables"].get(name)
        after = current["tables"].get(name)
        if before is None:
            problems.append(f"{name}: baseline 缺失，当前存在")
            continue
        if after is None:
            problems.append(f"{name}: 当前缺失，baseline 存在")
            continue
        if before["sha256"] != after["sha256"]:
            problems.append(
                f"{name}: 内容漂移 rows {before['rows']} -> {after['rows']} "
                f"sha256 {before['sha256'][:12]} -> {after['sha256'][:12]}"
            )
    if baseline.get("combined") != current.get("combined"):
        problems.append("combined: 冻结表整体哈希漂移")

    before_profile = baseline.get("generationProfile") or {}
    after_profile = current.get("generationProfile") or {}
    for key in sorted(set(before_profile) | set(after_profile)):
        if before_profile.get(key) != after_profile.get(key):
            problems.append(
                f"generationProfile.{key}: {before_profile.get(key)!r} -> "
                f"{after_profile.get(key)!r}"
            )
    return problems


def assert_generation_complete(profile: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in REQUIRED_PROFILE_KEYS:
        if not str(profile.get(key) or "").strip():
            problems.append(f"{key} 为空，无法证明三组生成参数一致")
    digest = str(profile.get("baseSystemInstructionSha256") or "")
    if len(digest) != 64:
        problems.append("baseSystemInstructionSha256 不是 SHA-256")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?", help="setup_snapshot.py 输出的快照清单")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write-baseline", action="store_true", help="写入基线")
    action.add_argument("--check", action="store_true", help="与基线比对（默认动作）")
    action.add_argument("--dump", action="store_true", help="只读打印当前哈希")
    action.add_argument("--self-test", action="store_true", help="不连库自检")
    parser.add_argument("--assert-generation", action="store_true", help="额外校验生成参数完整")
    parser.add_argument("--json-out", help="把结果写入 JSON 文件")
    args = parser.parse_args()

    if args.self_test:
        result = self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["problems"] else 1
    if not args.snapshot:
        parser.error("需要快照清单路径（或使用 --self-test）")

    snapshot_path = Path(args.snapshot)
    snapshot = load_snapshot(snapshot_path)
    baseline_path = default_baseline_path(snapshot_path)

    if args.write_baseline:
        baseline = write_baseline(snapshot_path, snapshot)
        print(f"baseline 已写入 {baseline_path}")
        print(json.dumps(baseline["tables"], ensure_ascii=False, indent=2))
        print(f"combined = {baseline['combined']}")
        result: dict[str, Any] = {"action": "write-baseline", "baseline": str(baseline_path)}
        exit_code = 0
    else:
        current = current_state(snapshot)
        if args.assert_generation:
            profile_problems = assert_generation_complete(current["generationProfile"])
            if profile_problems:
                print("生成参数不完整：", *profile_problems, sep="\n- ")
                return 1
        if args.dump:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            result = {"action": "dump", "current": current}
            exit_code = 0
        else:
            if not baseline_path.exists():
                raise SystemExit(f"基线不存在：{baseline_path}；先运行 --write-baseline")
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            problems = diff_baseline(baseline, current)
            if problems:
                print("冻结检查 FAIL：")
                for problem in problems:
                    print(f"- {problem}")
                exit_code = 1
            else:
                print("冻结检查 PASS：所有冻结表与生成参数与基线一致。")
                print(f"combined = {current['combined']}")
                exit_code = 0
            result = {
                "action": "check",
                "problems": problems,
                "currentCombined": current["combined"],
            }

    if args.json_out:
        write_json(Path(args.json_out), result)
    return exit_code


def _fake_snapshot() -> dict[str, Any]:
    return {
        "kind": "phase5-pre-step3-snapshot",
        "fixtureMarker": FIXTURE_MARKER,
        "isolation": {
            "db": "phase5_pilot_20261109120000_abcdef",
            "collectionPrefix": "phase5-pilot-20261109120000-abcdef",
            "memoryCollectionPrefix": "phase5-pilot-20261109120000-abcdef-memory",
        },
        "dataset": {
            "userId": 11,
            "spaceId": 22,
            "conceptId": 33,
            "sessionId": 44,
            "conversationId": 55,
            "historyMessageIds": [66, 67],
            "understandingId": 77,
            "revisionId": 88,
            "evidenceId": 99,
            "memoryId": 111,
        },
        "question": "中性问题",
    }


def _fake_specs() -> list[TableSpec]:
    return freeze_tables(_fake_snapshot())


def self_test() -> dict[str, Any]:
    problems: list[str] = []
    specs = _fake_specs()
    sql = {spec.name: spec.select_sql for spec in specs}

    if 'WHERE id = 11' not in sql["user"]:
        problems.append("user 查询未绑定 userId")
    if 'id IN (55)' not in sql["conversation"]:
        problems.append("conversation 查询未绑定 conversationId")
    if 'id IN (66,67)' not in sql["message"]:
        problems.append("message 查询未绑定历史消息 ID")
    if "last_message_seq" in sql["conversation"]:
        problems.append("conversation 哈希不应包含可变游标 last_message_seq")
    if "last_active_at" in sql["learning_session"]:
        problems.append("learning_session 哈希不应包含 last_active_at")
    if "active_content_hash" in sql["document"]:
        problems.append("document 不应包含生成列 active_content_hash")

    row_a = {"id": 1, "content": "同一条消息", "created_at": datetime(2026, 11, 9, 12, 0)}
    row_b = {"created_at": datetime(2026, 11, 9, 12, 0), "content": "同一条消息", "id": 1}
    if hash_rows([row_a]) != hash_rows([row_b]):
        problems.append("行哈希对字典键顺序不稳定")
    if hash_rows([row_a]) == hash_rows([{**row_a, "content": "不同"}]):
        problems.append("内容变化未改变行哈希")

    for broken in (
        {**_fake_snapshot()["isolation"], "db": "ails"},
        {**_fake_snapshot()["isolation"], "collectionPrefix": "ails_documents"},
    ):
        try:
            assert_isolated_state(broken)
        except SystemExit:
            pass
        else:
            problems.append(f"隔离断言漏放：{broken}")

    snapshot = _fake_snapshot()
    baseline = {
        "tables": {"user": {"rows": 1, "sha256": "a" * 64}},
        "combined": "a" * 64,
        "generationProfile": {"AILS_LLM_MODEL": "deepseek-chat"},
    }
    current = {
        "tables": {"user": {"rows": 1, "sha256": "b" * 64}},
        "combined": "b" * 64,
        "generationProfile": {"AILS_LLM_MODEL": "deepseek-chat"},
    }
    if not diff_baseline(baseline, current):
        problems.append("漂移未被检测")
    same = {
        "tables": {"user": {"rows": 1, "sha256": "a" * 64}},
        "combined": "a" * 64,
        "generationProfile": {"AILS_LLM_MODEL": "deepseek-chat"},
    }
    if diff_baseline(baseline, same):
        problems.append("相同基线被误判为漂移")

    if assert_generation_complete(
        {
            "AILS_LLM_PROVIDER": "deepseek",
            "AILS_LLM_MODEL": "deepseek-chat",
            "AILS_EMBEDDING_MODEL": "text-embedding-3-small",
            "AILS_EMBEDDING_DIMENSION": "256",
            "baseSystemInstructionSha256": "c" * 64,
        }
    ):
        problems.append("完整生成参数被误判")
    if not assert_generation_complete({"baseSystemInstructionSha256": "c" * 64}):
        problems.append("缺失生成参数未被检出")

    return {
        "tables": [spec.name for spec in specs],
        "datasetFingerprint": dataset_fingerprint(snapshot),
        "problems": problems,
    }


if __name__ == "__main__":
    sys.exit(main())
