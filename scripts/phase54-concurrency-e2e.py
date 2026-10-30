#!/usr/bin/env python3
"""Phase 5.4：Spring SSE + 真实模型的轻量并发基线。

这不是压测平台，也不声称 SLO：在单实例、单机、默认线程池配置下，同时发起
N 个独立会话的 SSE 请求，核对 HTTP/SSE 契约、首字/总耗时分布、落库内容一致、
`message.truncated=0`，以及每轮 inline 分析是否到达终态。

每个请求使用独立会话，避免同一会话的 sequence_no 互相竞争；所有业务数据通过
公开 API 产生，MySQL 只读核对。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
BACKEND_RESOURCES = ROOT / "backend" / "backend" / "src" / "main" / "resources"
DEFAULT_TIMEOUT = (10, 300)
PASSWORD = "Passw0rd!23"
QUESTION = "请用一句话解释 TLB 缓存的是什么。"


def load_db_credentials() -> tuple[str, str, str, str]:
    username = os.getenv("MYSQL_USER", "")
    password = os.getenv("MYSQL_PASSWORD", "")
    if not username or not password:
        for name in ("application-local.yml", "application.yml"):
            path = BACKEND_RESOURCES / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")

            def find(key: str) -> str:
                match = re.search(r"^\s*%s:\s*(.+?)\s*(?:#.*)?$" % re.escape(key), text, re.MULTILINE)
                if not match:
                    return ""
                raw = match.group(1).strip().strip('"').strip("'")
                env = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}", raw)
                return os.getenv(env.group(1), env.group(2) or "") if env else raw

            username = username or find("username")
            password = password or find("password")
            if username and password:
                break
    if not username or not password:
        raise RuntimeError("无法读取 MySQL 账号；请设置 MYSQL_USER/MYSQL_PASSWORD")
    return os.getenv("MYSQL_HOST", "127.0.0.1"), os.getenv("MYSQL_PORT", "3306"), username, password


class Sql:
    def __init__(self) -> None:
        host, port, user, password = load_db_credentials()
        self.args = ["mysql", f"-h{host}", f"-P{port}", f"-u{user}", "-N", "-B",
                     os.getenv("MYSQL_DB", "ails")]
        self.password = password

    def rows(self, statement: str) -> list[list[str]]:
        env = os.environ.copy()
        env["MYSQL_PWD"] = self.password
        completed = subprocess.run(self.args + ["-e", statement], env=env, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            reason = (completed.stderr or "").strip().splitlines()
            raise RuntimeError("MySQL 查询失败" + (f"：{reason[-1]}" if reason else ""))
        return [line.split("\t") for line in completed.stdout.splitlines() if line]


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def chat(base: str, token: str, conversation_id: int, index: int) -> dict[str, Any]:
    started = time.monotonic()
    events: list[str] = []
    deltas: list[str] = []
    first_delta_at: float | None = None
    current_event: str | None = None
    data_lines: list[str] = []
    payloads: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current_event, data_lines, first_delta_at
        if current_event is None:
            data_lines = []
            return
        event = current_event
        raw = "\n".join(data_lines)
        payload: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"raw": raw}
        events.append(event)
        payloads.append(payload)
        if event == "message.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                if first_delta_at is None:
                    first_delta_at = time.monotonic() - started
                deltas.append(delta)
        current_event = None
        data_lines = []

    error = ""
    status = 0
    try:
        response = requests.post(
            f"{base}/api/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            json={"content": QUESTION}, stream=True, timeout=DEFAULT_TIMEOUT,
        )
        status = response.status_code
        try:
            response.encoding = "utf-8"
            for raw in response.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.rstrip("\r")
                if line == "":
                    flush()
                elif line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            flush()
        finally:
            response.close()
    except Exception as exc:  # noqa: BLE001 —— 记录为失败请求，不中断整批
        error = f"{type(exc).__name__}: {exc}"

    return {
        "index": index,
        "conversationId": conversation_id,
        "httpStatus": status,
        "events": events,
        "payloads": payloads,
        "answer": "".join(deltas),
        "deltaCount": len(deltas),
        "firstDeltaMs": int(first_delta_at * 1000) if first_delta_at is not None else None,
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("BASE", "http://127.0.0.1:8081"))
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--total", type=int, default=12)
    parser.add_argument("--analysis-timeout", type=int, default=240)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "backend" / "backend" / "target" / "phase54-concurrency" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"（{detail}）" if detail else ""), flush=True)
        return bool(ok)

    print(f"产物目录：{out_dir}", flush=True)
    print(f"\n== 0. 准备 ==", flush=True)
    try:
        ready = requests.post(f"{base}/api/auth/login", json={}, timeout=5)
        if ready.status_code >= 500:
            raise RuntimeError(f"HTTP {ready.status_code}")
    except Exception as exc:
        check("Spring 可连接", False, f"{type(exc).__name__}: {exc}")
        return 1
    check("Spring 可连接", True, f"HTTP {ready.status_code}")

    def api(method: str, path: str, body: dict[str, Any] | None = None, token: str = "") -> Any:
        response = requests.request(method, urljoin(base, path),
                                    headers={"Authorization": f"Bearer {token}"},
                                    json=body, timeout=(10, 60))
        data = response.json()
        if response.status_code != 200 or data.get("code") != 0:
            raise RuntimeError(f"{method} {path} 失败：HTTP {response.status_code} {str(data)[:300]}")
        return data.get("data")

    user = api("POST", "/api/auth/register", {"username": f"p54load_{stamp}", "password": PASSWORD})
    token = user["token"]
    user_id = int(user["user"]["id"])
    space = api("POST", "/api/spaces", {"name": f"Phase54 并发 {stamp}", "description": "并发基线"}, token)
    space_id = int(space["id"])
    api("POST", f"/api/spaces/{space_id}/concepts",
        {"canonicalName": "TLB", "displayName": "TLB", "description": "页表项缓存"}, token)
    conversations = [
        int(api("POST", f"/api/spaces/{space_id}/conversations", {"title": f"并发-{i}"}, token)["id"])
        for i in range(args.total)
    ]
    check("创建独立会话", len(conversations) == args.total, f"{len(conversations)} 个")

    print(f"\n== 1. 并发 {args.concurrency} / 总数 {args.total} ==", flush=True)
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(chat, base, token, cid, i) for i, cid in enumerate(conversations)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    wall_ms = int((time.monotonic() - started) * 1000)
    results.sort(key=lambda item: item["index"])

    http_ok = sum(1 for r in results if r["httpStatus"] == 200)
    contract_ok = sum(1 for r in results
                      if r["events"][:1] == ["message.start"] and r["events"][-1:] == ["message.end"]
                      and "message.error" not in r["events"])
    nonempty = sum(1 for r in results if r["answer"].strip())
    firsts = [r["firstDeltaMs"] for r in results if r["firstDeltaMs"] is not None]
    totals = [r["elapsedMs"] for r in results]
    check("HTTP 200 全部成功", http_ok == args.total, f"{http_ok}/{args.total}")
    check("SSE 契约全部正常", contract_ok == args.total, f"{contract_ok}/{args.total}")
    check("回答全部非空", nonempty == args.total, f"{nonempty}/{args.total}")
    check("没有请求级异常", all(not r["error"] for r in results),
          "; ".join(r["error"] for r in results if r["error"])[:300])
    print(f"  首字 ms：p50={percentile(firsts, 0.5)} p95={percentile(firsts, 0.95)} max={max(firsts or [0])}", flush=True)
    print(f"  总耗时 ms：p50={percentile(totals, 0.5)} p95={percentile(totals, 0.95)} max={max(totals or [0])}", flush=True)
    print(f"  墙钟 ms：{wall_ms}", flush=True)

    print(f"\n== 2. 等待 inline 分析终态 ==", flush=True)
    sql = Sql()
    db_name = os.getenv("MYSQL_DB", "ails")
    deadline = time.monotonic() + args.analysis_timeout
    analysis: dict[int, dict[str, str]] = {}
    while time.monotonic() < deadline:
        rows = sql.rows(
            "SELECT conversation_id,id,status FROM agent_run "
            f"WHERE conversation_id IN ({','.join(str(c) for c in conversations)}) "
            "AND run_type='ANALYSIS' ORDER BY id"
        )
        analysis = {int(r[0]): {"id": r[1], "status": r[2]} for r in rows}
        if len(analysis) == args.total and all(
            item["status"] in {"COMPLETED", "FAILED", "TRUNCATED"} for item in analysis.values()
        ):
            break
        time.sleep(2)
    terminal = sum(1 for item in analysis.values() if item["status"] in {"COMPLETED", "FAILED", "TRUNCATED"})
    failed = sum(1 for item in analysis.values() if item["status"] == "FAILED")
    check("每个会话都有 ANALYSIS run 且到终态", terminal == args.total, f"{terminal}/{args.total}")
    check("没有 ANALYSIS FAILED", failed == 0, f"{failed} failed")

    print(f"\n== 3. 落库一致性 ==", flush=True)
    persisted_match = 0
    truncated_ok = 0
    message_ids: list[int] = []
    for result in results:
        cid = result["conversationId"]
        row = sql.rows(
            "SELECT id,REPLACE(TO_BASE64(content),'\\n',''),truncated FROM message "
            f"WHERE conversation_id={cid} AND role='ASSISTANT' AND message_type='TEXT' "
            "ORDER BY id DESC LIMIT 1"
        )
        if not row:
            continue
        message_id = int(row[0][0])
        content = base64.b64decode(row[0][1]).decode("utf-8")
        truncated = int(row[0][2])
        message_ids.append(message_id)
        if content == result["answer"]:
            persisted_match += 1
        if truncated == 0:
            truncated_ok += 1
        result["messageId"] = message_id
    check("落库内容与 SSE 拼接逐字一致", persisted_match == args.total, f"{persisted_match}/{args.total}")
    check("message.truncated 全部为 0", truncated_ok == args.total, f"{truncated_ok}/{args.total}")

    summary = {
        "stamp": stamp, "base": base, "db": db_name, "userId": user_id, "spaceId": space_id,
        "concurrency": args.concurrency, "total": args.total, "wallMs": wall_ms,
        "metrics": {
            "httpOk": http_ok, "contractOk": contract_ok, "nonEmpty": nonempty,
            "firstDeltaMs": {"p50": percentile(firsts, 0.5), "p95": percentile(firsts, 0.95),
                             "max": max(firsts or [0])},
            "elapsedMs": {"p50": percentile(totals, 0.5), "p95": percentile(totals, 0.95),
                          "max": max(totals or [0]), "median": int(median(totals))},
        },
        "analysis": analysis, "messageIds": message_ids,
        "pass": sum(1 for c in checks if c["ok"]), "fail": sum(1 for c in checks if not c["ok"]),
        "checks": checks, "results": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果：PASS={summary['pass']} FAIL={summary['fail']}")
    print(f"产物目录：{out_dir}")
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
