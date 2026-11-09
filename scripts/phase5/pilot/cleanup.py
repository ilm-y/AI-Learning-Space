"""Phase 5 Pilot 清理：停进程、删隔离 MySQL 库、删隔离 Qdrant collection、删 Pilot MQ 队列。

不删除 temp 目录中的日志/结果文件；清理失败会明确记录。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect_mysql, read_state, sanitize_error, write_json  # noqa: E402

QDRANT = "http://127.0.0.1:6333"
RABBIT = "http://127.0.0.1:15672"
RABBIT_AUTH = ("guest", "guest")


def stop_process(pid: int) -> dict:
    if not pid:
        return {"pid": pid, "stopped": True, "detail": "no pid"}
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "pid": pid,
            "stopped": completed.returncode == 0,
            "detail": (completed.stdout or completed.stderr).strip()[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {"pid": pid, "stopped": False, "detail": sanitize_error(exc)}


def drop_database(name: str) -> dict:
    try:
        conn = connect_mysql(None)
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            conn.commit()
        finally:
            conn.close()
        conn = connect_mysql(None)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SHOW DATABASES LIKE %s", (name,))
                remaining = cursor.fetchall()
        finally:
            conn.close()
        return {"dropped": True, "remaining": len(remaining)}
    except Exception as exc:  # noqa: BLE001
        return {"dropped": False, "error": sanitize_error(exc)}


def list_phase5_pilot_collections() -> list[str]:
    """列出所有本次实验命名空间下的 collection（文档 + memory）。

    只读；不按前缀模糊删除任何东西。返回空列表表示无残留。
    """
    listing = requests.get(f"{QDRANT}/collections", timeout=30).json()
    names = listing.get("result", {}).get("collections", [])
    return sorted(
        str(item.get("name"))
        for item in names
        if str(item.get("name", "")).startswith("phase5-pilot-")
    )


def delete_collection(name: str) -> dict:
    result = {"deleted": False, "gone": False}
    try:
        response = requests.delete(f"{QDRANT}/collections/{name}", timeout=30)
        result["deleteHttp"] = response.status_code
        result["deleted"] = response.status_code in (200, 202, 404)
        check = requests.get(f"{QDRANT}/collections/{name}", timeout=30)
        result["checkHttp"] = check.status_code
        result["gone"] = check.status_code == 404
        result["remainingPhase5Pilot"] = list_phase5_pilot_collections()
    except Exception as exc:  # noqa: BLE001
        result["error"] = sanitize_error(exc)
    return result


def delete_memory_collection(state: dict) -> dict:
    """删除本次 Pilot 独占的 memory collection。

    删除前必须通过隔离校验；绝不按 `ails_memories*` 前缀模糊删除，
    也绝不触碰共享默认 collection。
    """
    name = str(state.get("memoryCollection") or "")
    stamp = str(state.get("stamp") or "")
    default_name = str(state.get("defaultMemoryCollection") or "")
    checks = {
        "nonEmpty": bool(name),
        "phase5PilotPrefix": name.startswith("phase5-pilot-"),
        "hasStamp": bool(stamp) and stamp in name,
        "notDefault": bool(default_name) and name != default_name,
        "prefixNotSharedDefault": not name.startswith("ails_memories"),
    }
    result: dict = {"collection": name, "checks": checks, "deleted": False, "gone": False}
    if not all(checks.values()):
        result["refused"] = True
        result["reason"] = "memory collection 未通过隔离校验，拒绝删除"
    else:
        try:
            response = requests.delete(f"{QDRANT}/collections/{name}", timeout=30)
            result["deleteHttp"] = response.status_code
            result["deleted"] = response.status_code in (200, 202, 404)
            check = requests.get(f"{QDRANT}/collections/{name}", timeout=30)
            result["checkHttp"] = check.status_code
            result["gone"] = check.status_code == 404
        except Exception as exc:  # noqa: BLE001
            result["error"] = sanitize_error(exc)
    try:
        result["remainingPhase5Pilot"] = list_phase5_pilot_collections()
    except Exception as exc:  # noqa: BLE001
        result["residualCheckError"] = sanitize_error(exc)
    return result


def delete_rabbit(names: dict[str, str], stamp: str) -> dict:
    queues = [v for k, v in names.items() if k.endswith("QUEUE")]
    exchanges = [v for k, v in names.items() if k in {"AILS_ANALYSIS_EXCHANGE", "AILS_ANALYSIS_DLX"}]
    result: dict = {"queues": {}, "exchanges": {}, "leftovers": []}
    for name in queues:
        try:
            response = requests.delete(
                f"{RABBIT}/api/queues/%2F/{quote(name, safe='')}",
                auth=RABBIT_AUTH,
                timeout=15,
            )
            result["queues"][name] = response.status_code
        except Exception as exc:  # noqa: BLE001
            result["queues"][name] = sanitize_error(exc)
    for name in exchanges:
        try:
            response = requests.delete(
                f"{RABBIT}/api/exchanges/%2F/{quote(name, safe='')}",
                auth=RABBIT_AUTH,
                timeout=15,
            )
            result["exchanges"][name] = response.status_code
        except Exception as exc:  # noqa: BLE001
            result["exchanges"][name] = sanitize_error(exc)
    try:
        response = requests.get(
            f"{RABBIT}/api/queues/%2F", auth=RABBIT_AUTH, timeout=15
        )
        if response.status_code == 200:
            result["leftovers"] = sorted(
                item.get("name", "")
                for item in response.json()
                if str(item.get("name", "")).startswith(f"phase5.pilot.{stamp}")
            )
    except Exception as exc:  # noqa: BLE001
        result["leftoverCheckError"] = sanitize_error(exc)
    return result


def port_closed(port: int) -> bool:
    try:
        response = requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
        return False
    except requests.RequestException:
        return True


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: cleanup.py <STATE_FILE>")
    state = read_state(sys.argv[1])
    result: dict = {
        "stamp": state["stamp"],
        "db": state["db"],
        "collection": state["collection"],
        "memoryCollection": state.get("memoryCollection"),
        "defaultMemoryCollection": state.get("defaultMemoryCollection"),
    }

    result["springStop"] = stop_process(int(state["springPid"]))
    result["agentStop"] = stop_process(int(state["agentPid"]))
    time.sleep(2)
    result["portsClosed"] = {
        "agent": port_closed(int(state["agentPort"])),
        "spring": port_closed(int(state["springPort"])),
    }
    result["database"] = drop_database(state["db"])
    # 先删隔离 memory collection，再做全局残留检查（文档 collection 也会检查）。
    result["memoryCollectionCleanup"] = delete_memory_collection(state)
    result["collectionCleanup"] = delete_collection(state["collection"])
    result["rabbitmq"] = delete_rabbit(state.get("analysisNames", {}), state["stamp"])
    try:
        result["residualPhase5Collections"] = list_phase5_pilot_collections()
        residual_check_ok = True
    except Exception as exc:  # noqa: BLE001
        result["residualPhase5Collections"] = []
        result["residualCheckError"] = sanitize_error(exc)
        residual_check_ok = False

    memory_cleanup = result["memoryCollectionCleanup"]
    # 注意：memory_cleanup / collectionCleanup 里的 remainingPhase5Pilot 是在
    # 第二个 collection 删除之前计算的快照，不能拿来做最终判断；最终判据只用
    # 两次删除都完成后的 residualPhase5Collections。
    ok = (
        residual_check_ok
        and result["database"].get("dropped")
        and result["database"].get("remaining") == 0
        and memory_cleanup.get("gone")
        and result["collectionCleanup"].get("gone")
        and not result["residualPhase5Collections"]
        and not result["rabbitmq"].get("leftovers")
        and result["portsClosed"]["agent"]
        and result["portsClosed"]["spring"]
    )
    result["ok"] = bool(ok)
    write_json(Path(state["tempDir"]) / "cleanup-result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
