#!/usr/bin/env python3
"""阶段 5.3 服务端 E2E：Spring Boot → Python Agent → 真实 LLM。

只走服务端公开链路，不启动、不修改 Flutter 前端：

    登录 / 建空间 / 建概念 / 建会话
      → POST /api/conversations/{id}/messages（SSE）
      → Spring 转成 POST /agent/chat（Agent 内部 SSE）
      → 真实 OpenAI 兼容模型（默认 deepseek-flash）
      → Spring 持久化助手消息与 agent_run
      → inline 学习分析 POST /agent/analyze
      → Evidence / Understanding / Revision / LearningState 落库

脚本只通过公开 API 产生数据；MySQL 只用于只读核对。不会打印 API Key、
MySQL 密码或内部签名密钥。运行前需要：

    MySQL 可连；
    Agent 8000 已启动，AILS_LLM_PROVIDER=openai；
    后端 8081 已启动，AILS_AGENT_GATEWAY=http、AILS_ANALYSIS_MODE=inline，
    且与 Agent 使用相同的 AILS_INTERNAL_TOKEN / AILS_INTERNAL_IDENTITY_SECRET。

示例：
    AILS_INTERNAL_TOKEN=phase54-internal-token \
    AILS_INTERNAL_IDENTITY_SECRET=phase54-identity-secret \
    python scripts/phase53-spring-e2e.py \
      --base http://127.0.0.1:8081 --agent http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests


ROOT = Path(__file__).resolve().parent.parent
BACKEND_RESOURCES = ROOT / "backend" / "backend" / "src" / "main" / "resources"
DEFAULT_TIMEOUT = (10, 300)
ANALYSIS_TIMEOUT_SECONDS = 180


def _load_db_credentials() -> tuple[str, str, str, str]:
    """从 application-local.yml/application.yml 读取数据库账号，不打印密码。"""
    username = os.getenv("MYSQL_USER", "")
    password = os.getenv("MYSQL_PASSWORD", "")

    if not username or not password:
        for name in ("application-local.yml", "application.yml"):
            path = BACKEND_RESOURCES / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")

            def find(key: str) -> str:
                match = re.search(
                    r"^\s*%s:\s*(.+?)\s*(?:#.*)?$" % re.escape(key),
                    text,
                    re.MULTILINE,
                )
                if not match:
                    return ""
                raw = match.group(1).strip().strip('"').strip("'")
                env = re.fullmatch(
                    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}", raw
                )
                if env:
                    return os.getenv(env.group(1), env.group(2) or "")
                return raw

            username = username or find("username")
            password = password or find("password")
            if username and password:
                break

    if not username or not password:
        raise RuntimeError(
            "无法读取 MySQL 账号；请设置 MYSQL_USER/MYSQL_PASSWORD 或检查 application-local.yml"
        )
    return (
        os.getenv("MYSQL_HOST", "127.0.0.1"),
        os.getenv("MYSQL_PORT", "3306"),
        username,
        password,
    )


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Run:
    base: str
    agent: str
    out_dir: Path
    db_host: str
    db_port: str
    db_user: str
    db_password: str
    db_name: str = "ails"
    token: str = ""
    user_id: int = 0
    analysis_timeout: int = ANALYSIS_TIMEOUT_SECONDS
    checks: list[CheckResult] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    # ---------- 通用工具 ----------

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(CheckResult(name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f"（{detail}）"
        print(line, flush=True)
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n== {title} ==", flush=True)

    def api(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token if token is not None else self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = requests.request(
            method,
            urljoin(self.base, path),
            headers=headers,
            json=body,
            timeout=timeout,
        )
        try:
            data = response.json()
        except ValueError:
            data = response.text
        return response.status_code, data

    def api_ok(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    ) -> Any:
        status, data = self.api(method, path, body, token=token, timeout=timeout)
        if status != 200 or not isinstance(data, dict) or data.get("code") != 0:
            raise RuntimeError(f"{method} {path} 失败：HTTP {status} {str(data)[:300]}")
        return data.get("data")

    def sql(self, statement: str) -> list[list[str]]:
        env = os.environ.copy()
        env["MYSQL_PWD"] = self.db_password
        completed = subprocess.run(
            [
                "mysql",
                f"-h{self.db_host}",
                f"-P{self.db_port}",
                f"-u{self.db_user}",
                "-N",
                "-B",
                self.db_name,
                "-e",
                statement,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            # stderr 可能带连接信息，只保留最后一行；不打印密码。
            reason = (completed.stderr or "").strip().splitlines()
            raise RuntimeError(
                "MySQL 查询失败" + (f"：{reason[-1]}" if reason else "")
            )
        return [line.split("\t") for line in completed.stdout.splitlines() if line]

    def sql_one(self, statement: str) -> list[str]:
        rows = self.sql(statement)
        return rows[0] if rows else []

    def wait_for(
        self,
        description: str,
        probe: Callable[[], bool],
        *,
        timeout: int = ANALYSIS_TIMEOUT_SECONDS,
        interval: float = 1.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if probe():
                    return True
            except Exception as exc:  # 轮询期间的临时失败不算终局
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(interval)
        if last_error:
            print(f"  [WAIT] {description}：{last_error}", flush=True)
        return False

    # ---------- API 场景 ----------

    def register(self, username: str) -> dict[str, Any]:
        status, data = self.api(
            "POST",
            "/api/auth/register",
            {"username": username, "password": "Passw0rd!23"},
            token="",
        )
        if status == 200 and isinstance(data, dict) and data.get("code") == 0:
            return data["data"]
        status, data = self.api(
            "POST",
            "/api/auth/login",
            {"username": username, "password": "Passw0rd!23"},
            token="",
        )
        if status != 200 or not isinstance(data, dict) or data.get("code") != 0:
            raise RuntimeError(f"注册/登录失败：HTTP {status} {str(data)[:300]}")
        return data["data"]

    def create_space(self, stamp: str) -> tuple[int, int, int]:
        space = self.api_ok(
            "POST",
            "/api/spaces",
            {"name": f"阶段5.3 服务端 E2E {stamp}", "description": "Spring→Agent→LLM 真实链路"},
        )
        space_id = int(space["id"])
        tlb = self.api_ok(
            "POST",
            f"/api/spaces/{space_id}/concepts",
            {
                "canonicalName": "TLB",
                "displayName": "TLB",
                "description": "页表项的高速缓存",
            },
        )
        page_table = self.api_ok(
            "POST",
            f"/api/spaces/{space_id}/concepts",
            {
                "canonicalName": "Page Table",
                "displayName": "Page Table",
                "description": "页表",
            },
        )
        return space_id, int(tlb["id"]), int(page_table["id"])

    def create_conversation(self, space_id: int, title: str) -> int:
        conversation = self.api_ok(
            "POST",
            f"/api/spaces/{space_id}/conversations",
            {"title": title},
        )
        return int(conversation["id"])

    def chat_stream(
        self,
        conversation_id: int,
        content: str,
        filename: str,
    ) -> dict[str, Any]:
        """用 streaming HTTP 读 Spring 的 SSE，并保存原始事件与解析结果。"""
        url = f"{self.base}/api/conversations/{conversation_id}/messages"
        started = time.monotonic()
        events: list[str] = []
        payloads: list[dict[str, Any]] = []
        deltas: list[str] = []
        raw_lines: list[str] = []
        first_delta_at: float | None = None
        current_event: str | None = None
        data_lines: list[str] = []

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

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={"content": content},
            stream=True,
            timeout=DEFAULT_TIMEOUT,
        )
        try:
            # Spring 的 SSE 响应只声明 text/event-stream，不带 charset。
            # requests 会按 ISO-8859-1 解码，中文就会变成乱码；显式按 UTF-8 读。
            response.encoding = "utf-8"
            content_type = response.headers.get("Content-Type", "")
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

        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = {
            "httpStatus": response.status_code,
            "contentType": content_type,
            "events": events,
            "payloads": payloads,
            "answer": "".join(deltas),
            "deltaCount": len(deltas),
            "firstDeltaMs": int(first_delta_at * 1000) if first_delta_at is not None else None,
            "elapsedMs": elapsed_ms,
        }
        (self.out_dir / f"{filename}.sse").write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        (self.out_dir / f"{filename}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def wait_analysis_run(self, conversation_id: int) -> dict[str, Any] | None:
        """分析在 SSE 结束后由 chatStreamExecutor 启动；先等终态。"""
        def probe() -> bool:
            rows = self.sql(
                "SELECT id,status,IFNULL(output_summary,''),IFNULL(error_message,'') "
                f"FROM agent_run WHERE conversation_id={conversation_id} "
                "AND run_type='ANALYSIS' ORDER BY id DESC LIMIT 1"
            )
            if not rows:
                return False
            status = rows[0][1]
            return status in {"COMPLETED", "FAILED", "TRUNCATED"}

        if not self.wait_for("inline 分析 run 到终态", probe, timeout=self.analysis_timeout):
            return None
        row = self.sql_one(
            "SELECT id,status,IFNULL(output_summary,''),IFNULL(error_message,'') "
            f"FROM agent_run WHERE conversation_id={conversation_id} "
            "AND run_type='ANALYSIS' ORDER BY id DESC LIMIT 1"
        )
        return {
            "id": int(row[0]),
            "status": row[1],
            "outputSummary": row[2],
            "errorMessage": row[3],
        }

    def evidence_for_message(self, concept_id: int, message_id: int) -> list[dict[str, str]]:
        rows = self.sql(
            "SELECT e.id,e.analyzer_version,e.evidence_type,e.correctness,"
            "s.source_type,s.source_id "
            "FROM learning_evidence e JOIN evidence_source s ON s.evidence_id=e.id "
            f"WHERE e.concept_id={concept_id} AND s.source_type='MESSAGE' "
            f"AND s.source_id={message_id} ORDER BY e.id DESC"
        )
        return [
            {
                "id": row[0],
                "analyzerVersion": row[1],
                "evidenceType": row[2],
                "correctness": row[3],
                "sourceType": row[4],
                "sourceId": row[5],
            }
            for row in rows
        ]

    def get_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        data = self.api_ok("GET", f"/api/conversations/{conversation_id}/messages")
        return list(data or [])

    def get_understanding(self, concept_id: int) -> dict[str, Any]:
        return self.api_ok("GET", f"/api/understandings/{concept_id}")

    def highlight(self, message_id: int, mark_type: str, selected_text: str) -> dict[str, Any]:
        return self.api_ok(
            "POST",
            f"/api/messages/{message_id}/highlights",
            {"markType": mark_type, "selectedText": selected_text, "note": ""},
        )

    def preview_context(
        self, user_id: int, conversation_id: int, space_id: int, question: str
    ) -> dict[str, Any] | None:
        """可选：用与 Spring 相同的内部身份读取 Agent 注入的上下文区块。

        这不是聊天主链路；失败只记录 WARN，不计 FAIL。身份密钥只从环境变量取。
        """
        secret = os.getenv("AILS_INTERNAL_IDENTITY_SECRET", "")
        token = os.getenv("AILS_INTERNAL_TOKEN", "")
        if not secret or not token:
            return None
        env = os.environ.copy()
        env["AILS_INTERNAL_IDENTITY_SECRET"] = secret
        completed = subprocess.run(
            [sys.executable, "-m", "app.internal_identity", "--user", str(user_id)],
            cwd=ROOT / "agent",
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            return {"error": "无法签发内部身份"}
        identity = completed.stdout.strip()
        payload = {
            "runId": 540000 + conversation_id,
            "userId": user_id,
            "conversationId": conversation_id,
            "learningSpaceId": space_id,
            "userMessage": question,
            "authToken": self.token,
        }
        response = requests.post(
            f"{self.agent}/agent/context/preview",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": token,
                "X-Internal-Identity": identity,
                "X-Internal-User-Id": str(user_id),
            },
            json=payload,
            timeout=(10, 120),
        )
        if response.status_code != 200:
            return {"httpStatus": response.status_code, "error": response.text[:300]}
        data = response.json()
        if not isinstance(data, dict) or data.get("code") not in (0, None):
            return {"httpStatus": response.status_code, "error": str(data)[:300]}
        return data.get("data", data)


def _print_answer(answer: str, limit: int = 260) -> None:
    text = answer.replace("\n", "\n  ")
    print("  回答节选：")
    print("  " + text[:limit])
    if len(answer) > limit:
        print(f"  …（共 {len(answer)} 字）")


def _event_names(result: dict[str, Any]) -> str:
    names = result["events"]
    return " ".join(names[:4]) + (" …" if len(names) > 4 else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("BASE", "http://127.0.0.1:8081"))
    parser.add_argument("--agent", default=os.getenv("AGENT", "http://127.0.0.1:8000"))
    parser.add_argument("--analysis-timeout", type=int, default=ANALYSIS_TIMEOUT_SECONDS)
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "backend" / "backend" / "target" / "phase53-spring-e2e" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    db_host, db_port, db_user, db_password = _load_db_credentials()
    run = Run(
        base=args.base.rstrip("/"),
        agent=args.agent.rstrip("/"),
        out_dir=out_dir,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        db_name=os.getenv("MYSQL_DB", "ails"),
        analysis_timeout=args.analysis_timeout,
    )

    # 0. 前置检查
    run.section("0. 前置检查")
    try:
        health = requests.get(f"{run.agent}/health", timeout=5)
        run.check("Agent 存活", health.status_code == 200, f"HTTP {health.status_code}")
        diag = requests.get(
            f"{run.agent}/health/diagnostics",
            headers={"X-Internal-Token": os.getenv("AILS_INTERNAL_TOKEN", "")},
            timeout=5,
        ).json()
        provider = diag.get("llm_provider")
        model = diag.get("llm_model")
        run.check("Agent 使用真实模型", provider == "openai", f"provider={provider} model={model}")
        run.check("Agent 模型为 deepseek-flash", model == "deepseek-flash", f"model={model}")
    except Exception as exc:
        run.check("Agent 前置检查", False, f"{type(exc).__name__}: {str(exc)[:160]}")
        print("无法连接真实模型 Agent，停止。", file=sys.stderr)
        return 2

    try:
        run.sql("SELECT 1")
        run.check("MySQL 可读", True)
    except Exception as exc:
        run.check("MySQL 可读", False, str(exc)[:200])
        return 2

    login = run.register(f"p53svc_{stamp}")
    run.token = login["token"]
    run.user_id = int(login["user"]["id"])
    run.check("注册并取得 JWT", bool(run.token))

    space_id, tlb_id, _ = run.create_space(stamp)
    run.check("创建空间与概念", space_id > 0 and tlb_id > 0, f"space={space_id} TLB={tlb_id}")

    # 1. 普通问题：SSE 契约 + 真实模型 + 消息/run 落库 + inline 分析
    run.section("1. 普通问题：Spring SSE → Agent → LLM")
    conv_normal = run.create_conversation(space_id, "服务端 E2E：普通问题")
    question = "TLB 缓存的是什么？请用一两句话说明。"
    normal = run.chat_stream(conv_normal, question, "normal")
    run.artifacts["normal"] = normal
    run.check("HTTP 200", normal["httpStatus"] == 200, f"HTTP {normal['httpStatus']}")
    run.check("响应是 text/event-stream", "text/event-stream" in normal["contentType"], normal["contentType"])
    run.check("首事件是 message.start", normal["events"][:1] == ["message.start"], _event_names(normal))
    run.check("末事件是 message.end", normal["events"][-1:] == ["message.end"], _event_names(normal))
    run.check("没有 message.error", "message.error" not in normal["events"])
    run.check("真实模型有流式增量", normal["deltaCount"] > 0, f"deltas={normal['deltaCount']}")
    run.check("回答非空", len(normal["answer"]) > 0, f"chars={len(normal['answer'])}")
    start_payload = normal["payloads"][0] if normal["payloads"] else {}
    end_payload = normal["payloads"][-1] if normal["payloads"] else {}
    run_id = int(start_payload.get("runId", 0) or 0)
    message_id = int(end_payload.get("messageId", 0) or 0)
    run.check("message.start 带 runId", run_id > 0, str(run_id))
    run.check("message.end 带 messageId", message_id > 0, str(message_id))
    run.check("message.end 未标记截断", end_payload.get("truncated") is not True, str(end_payload))
    _print_answer(normal["answer"])

    messages = run.get_messages(conv_normal)
    assistants = [m for m in messages if m.get("role") == "ASSISTANT"]
    users = [m for m in messages if m.get("role") == "USER"]
    persisted = assistants[-1] if assistants else {}
    run.check("助手消息已落库", int(persisted.get("id", 0) or 0) == message_id, str(persisted.get("id")))
    run.check("落库内容与 SSE 拼接一致", persisted.get("content") == normal["answer"])
    run.check("落库消息未截断", persisted.get("truncated") is not True, str(persisted.get("truncated")))
    user_msg_id = int(users[-1]["id"]) if users else 0
    run.check("用户消息已落库", user_msg_id > 0, str(user_msg_id))

    run_row = run.sql_one(
        "SELECT status,IFNULL(output_summary,''),IFNULL(trace_id,''),IFNULL(duration_ms,0) "
        f"FROM agent_run WHERE id={run_id} AND run_type='CHAT'"
    )
    run.check(
        "CHAT agent_run=COMPLETED",
        bool(run_row) and run_row[0] == "COMPLETED",
        f"status={run_row[0] if run_row else 'missing'}",
    )
    run.check("CHAT run 有 trace_id", bool(run_row) and bool(run_row[2]), run_row[2][:12] if run_row else "")
    run.check(
        "CHAT run 有耗时",
        bool(run_row) and int(run_row[3] or 0) >= 0,
        f"{run_row[3] if run_row else '?'}ms",
    )
    db_truncated = run.sql_one(f"SELECT IFNULL(truncated,0) FROM message WHERE id={message_id}")
    run.check("数据库 message.truncated=0", bool(db_truncated) and db_truncated[0] == "0", str(db_truncated))

    analysis = run.wait_analysis_run(conv_normal)
    run.artifacts["normalAnalysis"] = analysis
    run.check("inline 分析 run 已完成", bool(analysis) and analysis["status"] == "COMPLETED", str(analysis))
    evidence = run.evidence_for_message(tlb_id, user_msg_id)
    run.artifacts["normalEvidence"] = evidence
    run.check("分析产生 MESSAGE 来源 Evidence", bool(evidence), f"count={len(evidence)}")
    run.check(
        "Evidence 来自 Agent 分析器（agent-v1）",
        bool(evidence) and evidence[0]["analyzerVersion"] == "agent-v1",
        evidence[0]["analyzerVersion"] if evidence else "missing",
    )
    if analysis:
        run.check(
            "ANALYSIS run 有输出摘要",
            bool(analysis["outputSummary"]),
            analysis["outputSummary"],
        )

    # 2. 错误解释 → 误区快照。这个场景用来给显式复盘提供真实的 Revision 历史。
    run.section("2. 错误解释：真实生成 + 误区证据")
    conv_wrong = run.create_conversation(space_id, "服务端 E2E：错误解释")
    wrong_text = "我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。"
    wrong = run.chat_stream(conv_wrong, wrong_text, "wrong")
    run.artifacts["wrong"] = wrong
    run.check("HTTP 200", wrong["httpStatus"] == 200, f"HTTP {wrong['httpStatus']}")
    run.check("首尾事件契约", wrong["events"][:1] == ["message.start"] and wrong["events"][-1:] == ["message.end"])
    run.check("没有 message.error", "message.error" not in wrong["events"])
    run.check("回答非空", len(wrong["answer"]) > 0, f"chars={len(wrong['answer'])}")
    _print_answer(wrong["answer"], 180)

    wrong_analysis = run.wait_analysis_run(conv_wrong)
    run.artifacts["wrongAnalysis"] = wrong_analysis
    run.check("错误解释的 inline 分析完成", bool(wrong_analysis) and wrong_analysis["status"] == "COMPLETED", str(wrong_analysis))
    wrong_messages = run.get_messages(conv_wrong)
    wrong_user_id = int([m for m in wrong_messages if m.get("role") == "USER"][-1]["id"])
    wrong_evidence = run.evidence_for_message(tlb_id, wrong_user_id)
    run.artifacts["wrongEvidence"] = wrong_evidence
    run.check("错误解释产生 Evidence", bool(wrong_evidence), f"count={len(wrong_evidence)}")

    hl = run.highlight(wrong_user_id, "MISCONCEPTION_CANDIDATE", "TLB 就是页表本身，它缓存的是整个页表")
    run.check("显式误区批注创建成功", int(hl.get("id", 0) or 0) > 0, str(hl.get("id")))
    misconception_ready = run.wait_for(
        "Understanding 进入 MISCONCEPTION",
        lambda: run.get_understanding(tlb_id).get("status") == "MISCONCEPTION",
        timeout=30,
    )
    run.check(
        "Understanding 进入 MISCONCEPTION",
        misconception_ready,
        run.get_understanding(tlb_id).get("status", "?"),
    )

    # 3. 显式复盘：让 Agent 通过 Spring 提供的 authToken 读取历史轨迹
    run.section("3. 显式复盘：Spring SSE → Agent 历史上下文 → LLM")
    conv_review = run.create_conversation(space_id, "服务端 E2E：显式复盘")
    review_question = "我之前对 TLB 的理解错在哪里？请只根据这次学习记录回答。"
    review = run.chat_stream(conv_review, review_question, "review")
    run.artifacts["review"] = review
    run.check("HTTP 200", review["httpStatus"] == 200, f"HTTP {review['httpStatus']}")
    run.check("首事件是 message.start", review["events"][:1] == ["message.start"], _event_names(review))
    run.check("末事件是 message.end", review["events"][-1:] == ["message.end"], _event_names(review))
    run.check("没有 message.error", "message.error" not in review["events"])
    run.check("真实模型有流式增量", review["deltaCount"] > 0, f"deltas={review['deltaCount']}")
    run.check("回答非空", len(review["answer"]) > 0, f"chars={len(review['answer'])}")
    _print_answer(review["answer"], 300)

    review_messages = run.get_messages(conv_review)
    review_assistant = [m for m in review_messages if m.get("role") == "ASSISTANT"][-1]
    review_user = [m for m in review_messages if m.get("role") == "USER"][-1]
    run.check("复盘助手消息已落库", review_assistant.get("content") == review["answer"])
    review_run_id = int((review["payloads"][0] if review["payloads"] else {}).get("runId", 0) or 0)
    review_run = run.sql_one(
        "SELECT status,IFNULL(trace_id,''),IFNULL(duration_ms,0) "
        f"FROM agent_run WHERE id={review_run_id} AND run_type='CHAT'"
    )
    run.check("复盘 CHAT run=COMPLETED", bool(review_run) and review_run[0] == "COMPLETED", str(review_run))
    review_analysis = run.wait_analysis_run(conv_review)
    run.artifacts["reviewAnalysis"] = review_analysis
    run.check("复盘 inline 分析完成", bool(review_analysis) and review_analysis["status"] == "COMPLETED", str(review_analysis))
    review_evidence = run.evidence_for_message(tlb_id, int(review_user["id"]))
    run.artifacts["reviewEvidence"] = review_evidence
    if review_evidence:
        run.check("复盘轮次的 Evidence 可追溯", True, f"count={len(review_evidence)}")
    else:
        print(
            "  [WARN] 复盘轮次没有新增 Evidence（可能被去重或本轮没有学习信号），不计 FAIL。",
            flush=True,
        )

    # 辅助检查：普通问题不注入轨迹，显式复盘注入轨迹。失败只记录，不影响主链路结论。
    run.section("4. 辅助检查：上下文区块（非聊天主链路）")
    normal_preview = run.preview_context(run.user_id, conv_normal, space_id, question)
    review_preview = run.preview_context(run.user_id, conv_review, space_id, review_question)
    run.artifacts["normalPreview"] = normal_preview
    run.artifacts["reviewPreview"] = review_preview

    def section_names(preview: dict[str, Any] | None) -> list[str]:
        if not preview or "error" in preview:
            return []
        context = preview.get("context") or {}
        return [str(s.get("name", "")) for s in context.get("sections", [])]

    normal_sections = section_names(normal_preview)
    review_sections = section_names(review_preview)
    print(f"  普通问题上下文：{normal_sections}", flush=True)
    print(f"  显式复盘上下文：{review_sections}", flush=True)
    preview_error = (
        (normal_preview or {}).get("error")
        or (review_preview or {}).get("error")
        if isinstance(normal_preview, dict) and isinstance(review_preview, dict)
        else None
    )
    if normal_preview is None or review_preview is None or preview_error:
        print(
            "  [WARN] 上下文区块检查未完成（缺少内部凭据或 preview 失败），不计入主链路结论。"
            + (f" {preview_error}" if preview_error else ""),
            flush=True,
        )
    else:
        run.check("普通问题不注入历史轨迹", not any(s.startswith("trajectory:") for s in normal_sections))
        run.check("显式复盘注入历史轨迹", any(s.startswith("trajectory:") for s in review_sections))

    # 5. 汇总
    run.section("5. 汇总")
    passed = sum(1 for c in run.checks if c.ok)
    failed = len(run.checks) - passed
    summary = {
        "stamp": stamp,
        "base": run.base,
        "agent": run.agent,
        "userId": run.user_id,
        "spaceId": space_id,
        "conceptId": tlb_id,
        "conversations": {
            "normal": conv_normal,
            "wrong": conv_wrong,
            "review": conv_review,
        },
        "pass": passed,
        "fail": failed,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in run.checks],
        "artifacts": run.artifacts,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果：PASS={passed} FAIL={failed}")
    print(f"产物目录：{out_dir}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
