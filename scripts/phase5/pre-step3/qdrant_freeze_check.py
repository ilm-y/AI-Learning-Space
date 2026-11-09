"""Qdrant 只读冻结检查：证明 Knowledge collection 在三次运行前后未变化。

决策 4：只记录，不写入、不删除。

- ``--write-baseline``：记录 collection 的 point 总数、每个 point 的 id 与
  payload 规范化 SHA-256，以及整体哈希；
- ``--check``：重新只读计算并与基线比对；计数、id 集合或 payload 任一变化
  即非零退出，并打印差异表；
- ``--dump``：只读打印当前值，便于人工排查；
- ``--self-test``：不连 Qdrant 的纯函数自检。

Qdrant 的 scroll 使用 HTTP POST，但它是**只读查询**（不创建/更新/删除任何
point 或 collection）。本脚本只发送 GET 与 scroll POST，不调用任何写接口。

本脚本不打印 payload 正文，只打印 id 与哈希；不读取、不输出任何密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from snapshot_common import (
    AGENT_DIR,
    assert_isolated_state,
    write_json,
)

try:  # snapshot_common 已把 pilot 目录加入 sys.path
    from common import parse_env_file
except ImportError:  # pragma: no cover - 仅在非常规导入路径下发生
    from scripts.phase5.pilot.common import parse_env_file  # type: ignore

PAGE_SIZE = 256
SCROLL_PATH = "/collections/{name}/points/scroll"


def load_snapshot(path: Path) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("kind") != "phase5-pre-step3-snapshot":
        raise SystemExit(f"不是 pre-Step3 快照清单：{path}")
    assert_isolated_state(snapshot["isolation"])
    return snapshot


def default_baseline_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(".qdrant-baseline.json")


def collection_name(snapshot: dict[str, Any]) -> str:
    env = snapshot.get("env") or {}
    name = str(env.get("docCollection") or env.get("collection") or "")
    prefix = str(snapshot["isolation"]["collectionPrefix"])
    if not name:
        raise SystemExit("快照清单缺少 env.docCollection，无法核对 Qdrant")
    if not name.startswith(prefix + "_"):
        raise SystemExit(
            f"Knowledge collection 不以隔离前缀开头：{name}（prefix={prefix}）"
        )
    memory_prefix = str(snapshot["isolation"]["memoryCollectionPrefix"])
    if name.startswith(memory_prefix + "_") or name == memory_prefix:
        raise SystemExit(f"拒绝把 Memory collection 当作 Knowledge 核对：{name}")
    return name


def canonical_payload(payload: Any) -> str:
    """payload 的稳定序列化；None 与空对象都规范化为 ``null`` / ``{}`` 原样。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def point_hash(point_id: Any, payload: Any) -> str:
    body = json.dumps(
        {"id": point_id, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def combined_hash(points: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for key in sorted(points):
        digest.update(key.encode("utf-8"))
        digest.update(b"=")
        digest.update(points[key].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_state(collection: str, points: dict[str, str], count: int) -> dict[str, Any]:
    return {
        "collection": collection,
        "pointCount": count,
        "points": dict(sorted(points.items())),
        "combined": combined_hash(points),
    }


def diff_state(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if baseline.get("collection") != current.get("collection"):
        problems.append(
            f"collection: {baseline.get('collection')!r} -> {current.get('collection')!r}"
        )
    if baseline.get("pointCount") != current.get("pointCount"):
        problems.append(
            f"pointCount: {baseline.get('pointCount')} -> {current.get('pointCount')}"
        )

    before = baseline.get("points") or {}
    after = current.get("points") or {}
    missing = sorted(set(before) - set(after))
    extra = sorted(set(after) - set(before))
    changed = sorted(
        key for key in set(before) & set(after) if before[key] != after[key]
    )
    if missing:
        problems.append(f"缺失 point：{missing[:20]}（共 {len(missing)}）")
    if extra:
        problems.append(f"新增 point：{extra[:20]}（共 {len(extra)}）")
    if changed:
        problems.append(f"payload 变化 point：{changed[:20]}（共 {len(changed)}）")
    if baseline.get("combined") != current.get("combined"):
        problems.append("combined: 整体哈希漂移")
    return problems


def qdrant_settings() -> tuple[str, dict[str, str]]:
    env = parse_env_file(AGENT_DIR / ".env")
    url = (env.get("AILS_QDRANT_URL") or "http://127.0.0.1:6333").rstrip("/")
    api_key = (env.get("AILS_QDRANT_API_KEY") or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    return url, headers


def fetch_collection_info(base: str, headers: dict[str, str], collection: str) -> dict[str, Any]:
    response = requests.get(f"{base}/collections/{collection}", headers=headers, timeout=30)
    if response.status_code == 404:
        raise SystemExit(f"Qdrant collection 不存在：{collection}")
    if response.status_code >= 400:
        raise SystemExit(f"读取 Qdrant collection 失败：HTTP {response.status_code}")
    data = response.json()
    result = data.get("result")
    if not isinstance(result, dict):
        raise SystemExit("Qdrant collection 响应缺少 result")
    return result


def scroll_all(
    base: str, headers: dict[str, str], collection: str
) -> tuple[dict[str, str], int]:
    """只读翻页；返回 {id字符串: payload哈希} 与实际读取到的 point 数。"""
    points: dict[str, str] = {}
    offset: Any = None
    fetched = 0
    while True:
        body: dict[str, Any] = {
            "limit": PAGE_SIZE,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        response = requests.post(
            base + SCROLL_PATH.format(name=collection),
            headers=headers,
            json=body,
            timeout=30,
        )
        if response.status_code >= 400:
            raise SystemExit(f"Qdrant scroll 失败：HTTP {response.status_code}")
        result = response.json().get("result") or {}
        for point in result.get("points") or []:
            point_id = point.get("id")
            key = str(point_id)
            if key in points:
                raise SystemExit(f"Qdrant scroll 返回重复 point id：{key}")
            points[key] = point_hash(point_id, point.get("payload"))
            fetched += 1
        offset = result.get("next_page_offset")
        if offset is None:
            break
        if fetched > 100_000:
            raise SystemExit("Qdrant scroll 超过 100000 点，拒绝继续（疑似异常集合）")
    return points, fetched


def current_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    collection = collection_name(snapshot)
    base, headers = qdrant_settings()
    info = fetch_collection_info(base, headers, collection)
    points, fetched = scroll_all(base, headers, collection)
    reported = int(info.get("points_count") or 0)
    if reported != fetched:
        raise SystemExit(
            f"Qdrant pointCount 与 scroll 结果不一致："
            f"points_count={reported}, scrolled={fetched}"
        )
    return build_state(collection, points, fetched)


def write_baseline(snapshot_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    state = current_state(snapshot)
    baseline = {
        "schemaVersion": 1,
        "kind": "phase5-pre-step3-qdrant-baseline",
        "snapshotPath": str(snapshot_path),
        "writtenAt": datetime.now().isoformat(timespec="seconds"),
        **state,
    }
    write_json(default_baseline_path(snapshot_path), baseline)
    return baseline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?", help="setup_snapshot.py 输出的快照清单")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write-baseline", action="store_true", help="写入 Qdrant 基线")
    action.add_argument("--check", action="store_true", help="与基线比对（默认动作）")
    action.add_argument("--dump", action="store_true", help="只读打印当前哈希")
    action.add_argument("--self-test", action="store_true", help="不连 Qdrant 自检")
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

    if args.write_baseline:
        baseline = write_baseline(snapshot_path, snapshot)
        print(f"Qdrant baseline 已写入 {default_baseline_path(snapshot_path)}")
        print(f"collection = {baseline['collection']}")
        print(f"pointCount = {baseline['pointCount']}")
        print(f"combined = {baseline['combined']}")
        result: dict[str, Any] = {
            "action": "write-baseline",
            "baseline": str(default_baseline_path(snapshot_path)),
            "pointCount": baseline["pointCount"],
            "combined": baseline["combined"],
        }
        exit_code = 0
    else:
        current = current_state(snapshot)
        if args.dump:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            result = {"action": "dump", "current": current}
            exit_code = 0
        else:
            baseline_path = default_baseline_path(snapshot_path)
            if not baseline_path.exists():
                raise SystemExit(
                    f"Qdrant 基线不存在：{baseline_path}；先运行 --write-baseline"
                )
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            problems = diff_state(baseline, current)
            if problems:
                print("Qdrant 冻结检查 FAIL：")
                for problem in problems:
                    print(f"- {problem}")
                exit_code = 1
            else:
                print(
                    "Qdrant 冻结检查 PASS：point 总数与 payload 哈希与基线一致。"
                )
                print(f"pointCount = {current['pointCount']}")
                print(f"combined = {current['combined']}")
                exit_code = 0
            result = {
                "action": "check",
                "problems": problems,
                "pointCount": current["pointCount"],
                "combined": current["combined"],
            }

    if args.json_out:
        write_json(Path(args.json_out), result)
    return exit_code


def self_test() -> dict[str, Any]:
    problems: list[str] = []
    payload_a = {"document_id": 1, "chunk_index": 0, "text": "TCP 三次握手"}
    payload_b = {"chunk_index": 0, "document_id": 1, "text": "TCP 三次握手"}
    if canonical_payload(payload_a) != canonical_payload(payload_b):
        problems.append("payload 规范化对字典键顺序不稳定")
    if point_hash(1, payload_a) != point_hash(1, payload_b):
        problems.append("point 哈希对 payload 键顺序不稳定")
    if point_hash(1, payload_a) == point_hash(2, payload_a):
        problems.append("point 哈希未绑定 id")
    if point_hash(1, payload_a) == point_hash(1, {**payload_a, "text": "不同"}):
        problems.append("payload 变化未改变 point 哈希")

    points_a = {"1": point_hash(1, payload_a), "2": point_hash(2, payload_b)}
    points_b = {"2": point_hash(2, payload_b), "1": point_hash(1, payload_a)}
    if combined_hash(points_a) != combined_hash(points_b):
        problems.append("整体哈希对 point 顺序不稳定")

    baseline = build_state("c", points_a, 2)
    if diff_state(baseline, build_state("c", points_b, 2)):
        problems.append("相同 collection 被误判为漂移")
    if not diff_state(baseline, build_state("c", {"1": points_a["1"]}, 1)):
        problems.append("缺失 point 未被检出")
    changed = {**points_a, "1": point_hash(1, {**payload_a, "text": "不同"})}
    if not diff_state(baseline, build_state("c", changed, 2)):
        problems.append("payload 变化未被检出")
    if not diff_state(baseline, build_state("other", points_a, 2)):
        problems.append("collection 变化未被检出")

    # 只读性静态检查：本文件不应出现 Qdrant 写接口。
    # 用拼接构造检测词，避免把检测词自身也算进源码。
    source = Path(__file__).read_text(encoding="utf-8")
    for forbidden in (
        "/points/" + "delete",
        "/points/" + "upsert",
        "requests." + "put(",
        "requests." + "delete(",
    ):
        if forbidden in source:
            problems.append(f"发现疑似写接口：{forbidden}")

    return {
        "problems": problems,
        "combined": combined_hash(points_a),
        "pointCount": len(points_a),
    }


if __name__ == "__main__":
    sys.exit(main())
