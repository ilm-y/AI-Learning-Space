#!/usr/bin/env python3
"""Phase 5.4：MQ 异步分析的真实模型 E2E。

与 ``scripts/phase51-mq-e2e.sh`` 的分工：
  - 5.1 脚本用 Mock LLM 验证 MQ 的 PENDING / 重试 / 死信机制；
  - 本脚本不重跑整套机制，只回答一个问题：**Worker 消费任务后，是否真的调用
    真实模型完成分析，并经 Result Consumer 落库**。顺带核对同一 taskId 的重复
    投递不会产生第二条 Evidence / Revision。

前置：Spring 8082（mq 模式、独立 exchange/queue）、Agent 8001（真实模型）已启动。
Worker 由脚本自己拉起和停止；Agent 不持有 delegation secret。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aio_pika
import requests
from aio_pika import DeliveryMode, Message

ROOT = Path(__file__).resolve().parents[1]
BACKEND_RESOURCES = ROOT / "backend" / "backend" / "src" / "main" / "resources"
DEFAULT_TIMEOUT = (10, 300)
PASSWORD = "Passw0rd!23"
WRONG = "我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。"
ANALYSIS_TIMEOUT = 240


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
        self.host, self.port, self.user, self.password = load_db_credentials()
        self.db = os.getenv("MYSQL_DB", "ails")

    def rows(self, statement: str) -> list[list[str]]:
        env = os.environ.copy()
        env["MYSQL_PWD"] = self.password
        completed = subprocess.run(
            ["mysql", f"-h{self.host}", f"-P{self.port}", f"-u{self.user}", "-N", "-B", self.db, "-e", statement],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if completed.returncode != 0:
            reason = (completed.stderr or "").strip().splitlines()
            raise RuntimeError("MySQL 查询失败" + (f"：{reason[-1]}" if reason else ""))
        return [line.split("\t") for line in completed.stdout.splitlines() if line]

    def one(self, statement: str) -> str:
        rows = self.rows(statement)
        return rows[0][0] if rows else ""


def wait_until(description: str, probe, timeout: int = ANALYSIS_TIMEOUT, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if probe():
                return True
        except Exception:
            pass
        time.sleep(interval)
    print(f"  [WAIT] {description} 超时", flush=True)
    return False


def chat(base: str, token: str, conversation_id: int, content: str, out_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    events: list[str] = []
    deltas: list[str] = []
    first_delta_at: float | None = None
    current_event: str | None = None
    data_lines: list[str] = []
    raw_lines: list[str] = []

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
        if event == "message.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                if first_delta_at is None:
                    first_delta_at = time.monotonic() - started
                deltas.append(delta)
        current_event = None
        data_lines = []

    response = requests.post(
        f"{base}/api/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        json={"content": content}, stream=True, timeout=DEFAULT_TIMEOUT,
    )
    try:
        response.encoding = "utf-8"
        for raw in response.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.rstrip("\r")
            raw_lines.append(line)
            if line == "":
                flush()
            elif line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush()
    finally:
        response.close()

    result = {
        "httpStatus": response.status_code, "events": events, "answer": "".join(deltas),
        "deltaCount": len(deltas), "firstDeltaMs": int(first_delta_at * 1000) if first_delta_at else None,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }
    (out_dir / "chat.sse").write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    (out_dir / "chat.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def publish_duplicate(task: dict[str, Any], exchange_name: str, routing_key: str) -> None:
    async def run() -> None:
        connection = await aio_pika.connect_robust(os.getenv("AILS_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/"))
        channel = await connection.channel()
        exchange = await channel.declare_exchange(exchange_name, aio_pika.ExchangeType.DIRECT, durable=True)
        await exchange.publish(
            Message(body=json.dumps(task, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json", delivery_mode=DeliveryMode.PERSISTENT,
                    headers={"x-analysis-duplicate": True}),
            routing_key=routing_key, mandatory=True,
        )
        await connection.close()

    asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("BASE", "http://127.0.0.1:8082"))
    parser.add_argument("--agent", default=os.getenv("AGENT", "http://127.0.0.1:8001"))
    parser.add_argument("--backend-log", default=os.getenv("BACKEND_LOG", ""))
    args = parser.parse_args()
    base = args.base.rstrip("/")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "backend" / "backend" / "target" / "phase54-mq-real-e2e" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"（{detail}）" if detail else ""), flush=True)
        return bool(ok)

    print(f"产物目录：{out_dir}", flush=True)
    sql = Sql()
    worker: subprocess.Popen | None = None

    try:
        print("\n== 0. 前置 ==", flush=True)
        try:
            response = requests.post(f"{base}/api/auth/login", json={}, timeout=5)
            check("Spring 8082 可连接", response.status_code < 500, f"HTTP {response.status_code}")
        except Exception as exc:
            check("Spring 8082 可连接", False, f"{type(exc).__name__}: {exc}")
            return 1
        try:
            response = requests.get(f"{args.agent}/health", timeout=5)
            check("Agent 8001 可连接", response.status_code == 200, f"HTTP {response.status_code}")
        except Exception as exc:
            check("Agent 8001 可连接", False, f"{type(exc).__name__}: {exc}")
            return 1
        try:
            response = requests.get("http://127.0.0.1:15672/api/overview",
                                    auth=("guest", "guest"), timeout=5)
            check("RabbitMQ 管理接口可连接", response.status_code == 200, f"HTTP {response.status_code}")
        except Exception as exc:
            check("RabbitMQ 管理接口可连接", False, f"{type(exc).__name__}: {exc}")
            return 1

        def api(method: str, path: str, body: dict[str, Any] | None = None, token: str = "") -> Any:
            response = requests.request(method, urljoin(base, path),
                                        headers={"Authorization": f"Bearer {token}"},
                                        json=body, timeout=(10, 60))
            data = response.json()
            if response.status_code != 200 or data.get("code") != 0:
                raise RuntimeError(f"{method} {path} 失败：HTTP {response.status_code} {str(data)[:300]}")
            return data.get("data")

        user = api("POST", "/api/auth/register", {"username": f"p54mq_{stamp}", "password": PASSWORD})
        token = user["token"]
        user_id = int(user["user"]["id"])
        space = api("POST", "/api/spaces", {"name": f"Phase54 MQ {stamp}", "description": "真实模型 MQ"}, token)
        space_id = int(space["id"])
        concept = api("POST", f"/api/spaces/{space_id}/concepts",
                      {"canonicalName": "TLB", "displayName": "TLB", "description": "页表项缓存"}, token)
        concept_id = int(concept["id"])
        conv = api("POST", f"/api/spaces/{space_id}/conversations", {"title": "MQ 真实模型"}, token)
        conversation_id = int(conv["id"])
        check("注册 / 空间 / 概念 / 会话创建", bool(token and space_id and concept_id and conversation_id),
              f"user={user_id} space={space_id} concept={concept_id}")

        print("\n== 1. 回复结束，分析任务 PENDING ==", flush=True)
        chat_result = chat(base, token, conversation_id, WRONG, out_dir)
        check("聊天 SSE 正常结束", chat_result["httpStatus"] == 200
              and chat_result["events"][:1] == ["message.start"]
              and chat_result["events"][-1:] == ["message.end"]
              and "message.error" not in chat_result["events"],
              f"{chat_result['httpStatus']} / {len(chat_result['events'])} events")
        run_id = ""
        if wait_until("创建 ANALYSIS run", lambda: bool(
                sql.one(f"SELECT id FROM agent_run WHERE conversation_id={conversation_id} "
                        "AND run_type='ANALYSIS' ORDER BY id DESC LIMIT 1")), timeout=30):
            run_id = sql.one(f"SELECT id FROM agent_run WHERE conversation_id={conversation_id} "
                             "AND run_type='ANALYSIS' ORDER BY id DESC LIMIT 1")
        check("创建 ANALYSIS run", bool(run_id), f"runId={run_id}")
        check("Worker 未启动时保持 PENDING", bool(run_id) and sql.one(
            f"SELECT status FROM agent_run WHERE id={run_id}") == "PENDING",
            sql.one(f"SELECT status FROM agent_run WHERE id={run_id}") if run_id else "")
        check("Worker 未启动时没有 Evidence", sql.one(
            f"SELECT COUNT(*) FROM learning_evidence WHERE concept_id={concept_id}") == "0", "")

        messages = api("GET", f"/api/conversations/{conversation_id}/messages", token=token)
        message_id = next(int(m["id"]) for m in messages if m["role"] == "USER")

        print("\n== 2. 启动真实模型 Worker ==", flush=True)
        env = os.environ.copy()
        env.update({
            "AILS_INTERNAL_TOKEN": os.getenv("AILS_INTERNAL_TOKEN", ""),
            "AILS_INTERNAL_IDENTITY_SECRET": os.getenv("AILS_INTERNAL_IDENTITY_SECRET", ""),
            "AILS_BACKEND_BASE_URL": base,
            "AILS_ANALYSIS_EXCHANGE": os.getenv("AILS_ANALYSIS_EXCHANGE", "learning.analysis.phase54.exchange"),
            "AILS_ANALYSIS_DLX": os.getenv("AILS_ANALYSIS_DLX", "learning.analysis.phase54.dlx"),
            "AILS_ANALYSIS_REQUEST_QUEUE": os.getenv("AILS_ANALYSIS_REQUEST_QUEUE", "learning.analysis.phase54.request"),
            "AILS_ANALYSIS_REQUEST_ROUTING_KEY": "analysis.request",
            "AILS_ANALYSIS_RESULT_QUEUE": os.getenv("AILS_ANALYSIS_RESULT_QUEUE", "learning.analysis.phase54.result"),
            "AILS_ANALYSIS_RESULT_ROUTING_KEY": "analysis.result",
            "AILS_ANALYSIS_RETRY_QUEUE": os.getenv("AILS_ANALYSIS_RETRY_QUEUE", "learning.analysis.phase54.retry"),
            "AILS_ANALYSIS_RETRY_ROUTING_KEY": "analysis.retry",
            "AILS_ANALYSIS_DEAD_QUEUE": os.getenv("AILS_ANALYSIS_DEAD_QUEUE", "learning.analysis.phase54.dlq"),
            "AILS_ANALYSIS_DEAD_ROUTING_KEY": "analysis.dead",
            "PYTHONUNBUFFERED": "1",
        })
        env.pop("AILS_INTERNAL_DELEGATION_SECRET", None)
        worker_log = out_dir / "worker.log"
        worker = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT / "agent", env=env,
                                  stdout=worker_log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT)
        started = wait_until("Worker 启动", lambda: "分析 Worker 已启动" in worker_log.read_text(
            encoding="utf-8", errors="replace"), timeout=40)
        check("Worker 已连接 RabbitMQ", started, "")

        # FAILED 也是终态：不要为了等 SUCCEEDED 白等满 240 秒，先把真实状态记下来。
        wait_until("ANALYSIS run 到达终态", lambda: run_id and sql.one(
            f"SELECT status FROM agent_run WHERE id={run_id}") in {"SUCCEEDED", "FAILED"},
            timeout=ANALYSIS_TIMEOUT)
        status = sql.one(f"SELECT status FROM agent_run WHERE id={run_id}") if run_id else ""
        check("真实模型分析完成且 run SUCCEEDED", status == "SUCCEEDED", f"status={status}")
        log_text = worker_log.read_text(encoding="utf-8", errors="replace")
        check("Worker 日志证明调用了真实模型",
              "openai-compatible:deepseek-flash" in log_text,
              "LLM_CALL adapter openai-compatible:deepseek-flash")

        print("\n== 3. 结果落库 ==", flush=True)
        evidence_count = int(sql.one(f"SELECT COUNT(*) FROM learning_evidence WHERE concept_id={concept_id}") or 0)
        check("Evidence 已落库", evidence_count >= 1, f"{evidence_count} 条")
        check("Evidence 标记 agent-v1", sql.one(
            f"SELECT COUNT(*) FROM learning_evidence WHERE concept_id={concept_id} AND analyzer_version='agent-v1'") == str(evidence_count),
            "")
        source_count = sql.one(
            "SELECT COUNT(*) FROM learning_evidence e JOIN evidence_source s ON s.evidence_id=e.id "
            f"WHERE e.concept_id={concept_id} AND s.source_type='MESSAGE' AND s.source_id={message_id}")
        check("Evidence 可回跳到本轮用户消息", source_count == str(evidence_count), f"source={source_count}")
        understanding = sql.one(f"SELECT status FROM learning_understanding WHERE user_id={user_id} AND concept_id={concept_id}")
        revisions = sql.one(
            "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=("
            f"SELECT id FROM learning_understanding WHERE user_id={user_id} AND concept_id={concept_id})")
        check("Understanding / Revision 已落库", bool(understanding) and int(revisions or 0) >= 1,
              f"status={understanding} revisions={revisions}")

        print("\n== 4. 同 taskId 重复投递 ==", flush=True)
        delegation = os.getenv("AILS_INTERNAL_DELEGATION_SECRET", "")
        if not delegation:
            check("具备重复投递所需的 delegation secret", False, "未设置 AILS_INTERNAL_DELEGATION_SECRET")
            return 1
        identity_env = os.environ.copy()
        identity_env["AILS_INTERNAL_DELEGATION_SECRET"] = delegation
        minted = subprocess.run(
            [sys.executable, "-m", "app.internal_identity", "--user", str(user_id),
             "--audience", "learning-backend", "--scope", "analysis:read", "--ttl", "900"],
            cwd=ROOT / "agent", env=identity_env, capture_output=True, text=True, encoding="utf-8",
        )
        if minted.returncode != 0:
            check("签发后端委托身份", False, minted.stderr[-300:])
            return 1
        task = {
            "schemaVersion": 2, "taskId": int(run_id), "runId": int(run_id),
            "traceId": f"phase54-mq-{stamp}", "userId": user_id,
            "conversationId": conversation_id, "learningSpaceId": space_id,
            "messageId": message_id, "userMessage": WRONG, "history": [],
            "backendIdentity": minted.stdout.strip(),
        }
        exchange_name = os.getenv("AILS_ANALYSIS_EXCHANGE", "learning.analysis.phase54.exchange")
        publish_duplicate(task, exchange_name, "analysis.request")
        duplicate_seen = wait_until(
            "Worker 处理重复任务",
            lambda: worker_log.read_text(encoding="utf-8", errors="replace").count(
                f"分析完成, taskId={run_id}") >= 2,
            timeout=ANALYSIS_TIMEOUT,
        )
        check("Worker 处理了同一 taskId 的重复副本", duplicate_seen,
              f"count={worker_log.read_text(encoding='utf-8', errors='replace').count(f'分析完成, taskId={run_id}')}")
        time.sleep(3)
        evidence_after = sql.one(f"SELECT COUNT(*) FROM learning_evidence WHERE concept_id={concept_id}")
        revisions_after = sql.one(
            "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=("
            f"SELECT id FROM learning_understanding WHERE user_id={user_id} AND concept_id={concept_id})")
        check("重复任务没有新增 Evidence", evidence_after == str(evidence_count),
              f"before={evidence_count} after={evidence_after}")
        check("重复任务没有新增 Revision", revisions_after == revisions,
              f"before={revisions} after={revisions_after}")
        if args.backend_log:
            backend_text = Path(args.backend_log).read_text(encoding="utf-8", errors="replace")
            check("Result Consumer 识别重复结果", f"分析结果重复投递，已忽略, taskId={run_id}" in backend_text,
                  args.backend_log)
        else:
            print("  [WARN] 未提供 --backend-log，跳过 Result Consumer 日志断言", flush=True)

        summary = {
            "stamp": stamp, "base": base, "agent": args.agent, "db": os.getenv("MYSQL_DB", "ails"),
            "userId": user_id, "spaceId": space_id, "conceptId": concept_id,
            "conversationId": conversation_id, "runId": int(run_id), "messageId": message_id,
            "chat": chat_result, "workerLog": str(worker_log),
            "pass": sum(1 for c in checks if c["ok"]), "fail": sum(1 for c in checks if not c["ok"]),
            "checks": checks,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果：PASS={summary['pass']} FAIL={summary['fail']}")
        print(f"产物目录：{out_dir}")
        return 0 if summary["fail"] == 0 else 1
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()


if __name__ == "__main__":
    raise SystemExit(main())
