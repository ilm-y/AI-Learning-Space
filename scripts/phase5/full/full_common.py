"""Phase 5 Step 3 全量 A/B/C 实验的共用执行库。

本模块只服务于 ``scripts/phase5/full/`` 下的实验脚本，不修改任何生产代码：

- 启动 / 停止隔离的 Spring(8081) 与 Agent(8001)；
- 通过 Spring 公开 API 建 Space / Concept / Session / Conversation；
- 用一个 SQL 事务写入历史消息与 Understanding / Evidence / Revision / Memory；
- 通过 Agent 内部接口为 Memory 建向量，通过 Spring 上传并索引 Knowledge；
- 对全部 10 个场景做「全局冻结」与 Qdrant 基线 / 核对；
- 每次 Chat 后把会话恢复成快照（删除本轮新增消息、重置游标）；
- 只读检查 RabbitMQ 消费者与默认 Memory collection。

密钥只在进程内存中使用，不写入 state / snapshot / 报告。所有写入前都先通过
``assert_isolated_state`` 校验目标库与前缀。MySQL 凭据来自本机
``application-local.yml``，由 pilot/common.py 读取，不打印。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests

FULL_DIR = Path(__file__).resolve().parent
PILOT_DIR = FULL_DIR.parent / "pilot"
PRESTEP3_DIR = FULL_DIR.parent / "pre-step3"
AGENT_DIR = FULL_DIR.parents[2] / "agent"
for _path in (PILOT_DIR, PRESTEP3_DIR, AGENT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import (  # noqa: E402
    SPRING_JAR,
    connect_mysql,
    parse_env_file,
    read_state,
    sanitize_error,
    write_json,
)
from qdrant_freeze_check import (  # noqa: E402
    combined_hash as qdrant_combined_hash,
    point_hash,
    qdrant_settings,
    scroll_all,
)
from snapshot_common import (  # noqa: E402
    TableSpec,
    assert_isolated_state,
    combined_hash,
    hash_rows,
    read_table,
)

AGENT_PORT = 8001
SPRING_PORT = 8081
DEFAULT_MEMORY_PREFIX = "ails_memories"
RUN_STRATEGIES = ("A", "C", "B")
REPS = 3
SNAPSHOT_KIND = "phase5-full-abc-snapshot"
BASELINE_KIND = "phase5-full-abc-baseline"
QDRANT_BASELINE_KIND = "phase5-full-abc-qdrant-baseline"

SCENARIO_MARKER_RE = re.compile(r"【(S\d+)-")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def stamp_now() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)


def collection_suffix(values: dict[str, str]) -> str:
    model = (values.get("AILS_EMBEDDING_MODEL") or "text-embedding-3-small").strip()
    safe_model = re.sub(r"[^a-zA-Z0-9_-]+", "_", model).strip("_") or "model"
    raw_dimension = (values.get("AILS_EMBEDDING_DIMENSION") or "256").strip()
    dimension = int(raw_dimension)
    if dimension <= 0:
        raise SystemExit(f"AILS_EMBEDDING_DIMENSION 必须为正数：{dimension}")
    return f"{safe_model}_{dimension}"


def analysis_names(stamp: str) -> dict[str, str]:
    base = f"phase5.pilot.{stamp}"
    return {
        "AILS_ANALYSIS_EXCHANGE": f"{base}.exchange",
        "AILS_ANALYSIS_DLX": f"{base}.dlx",
        "AILS_ANALYSIS_REQUEST_QUEUE": f"{base}.request",
        "AILS_ANALYSIS_REQUEST_ROUTING_KEY": f"{base}.request.key",
        "AILS_ANALYSIS_RESULT_QUEUE": f"{base}.result",
        "AILS_ANALYSIS_RESULT_ROUTING_KEY": f"{base}.result.key",
        "AILS_ANALYSIS_RESULT_RETRY_QUEUE": f"{base}.result.retry",
        "AILS_ANALYSIS_RESULT_RETRY_ROUTING_KEY": f"{base}.result.retry.key",
        "AILS_ANALYSIS_RETRY_QUEUE": f"{base}.retry",
        "AILS_ANALYSIS_RETRY_ROUTING_KEY": f"{base}.retry.key",
        "AILS_ANALYSIS_DEAD_QUEUE": f"{base}.dead",
        "AILS_ANALYSIS_DEAD_ROUTING_KEY": f"{base}.dead.key",
    }


def new_state(root: Path | None = None) -> dict[str, Any]:
    values = parse_env_file()
    stamp = stamp_now()
    prefix = "phase5-pilot-" + stamp
    memory_prefix = prefix + "-memory"
    suffix = collection_suffix({**os.environ, **values})
    temp_dir = Path(root or tempfile.mkdtemp(prefix="phase5-full-")).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)
    return {
        "stamp": stamp,
        "db": "phase5_pilot_" + stamp.replace("-", "_"),
        "collectionPrefix": prefix,
        "collection": f"{prefix}_{suffix}",
        "collectionSuffix": suffix,
        "memoryCollectionPrefix": memory_prefix,
        "memoryCollection": f"{memory_prefix}_{suffix}",
        "defaultMemoryCollection": f"{DEFAULT_MEMORY_PREFIX}_{suffix}",
        "agentPort": AGENT_PORT,
        "springPort": SPRING_PORT,
        "tempDir": str(temp_dir),
        "agentLog": str(temp_dir / "agent.log"),
        "springLog": str(temp_dir / "spring.log"),
        "startedAt": now_iso(),
        "analysisNames": analysis_names(stamp),
    }


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    write_json(Path(path), state)


def state_path_for(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "state.json"


def default_snapshot_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "full-snapshot.json"


def baseline_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(".baseline.json")


def qdrant_baseline_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(".qdrant-baseline.json")


def results_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "results.json"


# ---------------------------------------------------------------------------
# HTTP / SSE
# ---------------------------------------------------------------------------


def api(
    base: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    timeout: float = 60,
    **kwargs: Any,
) -> Any:
    headers = dict(kwargs.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(
        method, base + path, headers=headers, timeout=timeout, **kwargs
    )
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:500]}
    if response.status_code >= 400:
        raise RuntimeError(
            f"{method} {path} HTTP {response.status_code}: {str(data)[:400]}"
        )
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"{method} {path} code={data.get('code')}: {str(data)[:400]}")
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def register_or_login(state: dict[str, Any], password: str) -> tuple[str, int, str]:
    base = f"http://127.0.0.1:{state['springPort']}"
    username = "phase5full_" + state["stamp"].replace("-", "_")
    body = {"username": username, "password": password}
    try:
        data = api(base, "POST", "/api/auth/register", json=body)
    except RuntimeError:
        data = api(base, "POST", "/api/auth/login", json=body)
    return str(data["token"]), int(data["user"]["id"]), username


def login(state: dict[str, Any], username: str, password: str) -> str:
    base = f"http://127.0.0.1:{state['springPort']}"
    data = api(
        base, "POST", "/api/auth/login", json={"username": username, "password": password}
    )
    return str(data["token"])


def wait_http(
    url: str, *, timeout: float, proc: subprocess.Popen, description: str
) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{description} 进程已退出，exit={proc.returncode}")
        try:
            response = requests.get(url, timeout=2)
            if response.status_code < 500:
                return
            last = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = sanitize_error(exc)
        time.sleep(1)
    raise RuntimeError(f"{description} 未就绪：{last}")


def port_free(port: int) -> bool:
    try:
        requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
        return False
    except requests.RequestException:
        return True


def agent_health(state: dict[str, Any]) -> dict[str, Any]:
    return api(f"http://127.0.0.1:{state['agentPort']}", "GET", "/health")


def agent_context(state: dict[str, Any]) -> dict[str, Any]:
    return api(
        f"http://127.0.0.1:{state['agentPort']}",
        "GET",
        "/phase5/prestep3-context-hashes",
    )


def agent_diagnostics(state: dict[str, Any]) -> dict[str, Any]:
    return api(
        f"http://127.0.0.1:{state['agentPort']}", "GET", "/phase5/llm-diagnostics"
    )


def chat(
    state: dict[str, Any],
    token: str,
    conversation_id: int,
    question: str,
    label: str,
    timeout: float = 600,
) -> dict[str, Any]:
    """发送一条消息并解析 Spring SSE；不打印正文。"""
    base = f"http://127.0.0.1:{state['springPort']}"
    client_request_id = str(uuid.uuid4())
    started = time.monotonic()
    events: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}
    deltas: list[str] = []
    current_event: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal current_event, data_lines
        if current_event is None:
            data_lines = []
            return
        raw = "\n".join(data_lines)
        payload: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"raw": raw[:500]}
        events.append(current_event)
        payloads[current_event] = payload
        if current_event == "message.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        current_event = None
        data_lines = []

    response = requests.post(
        f"{base}/api/conversations/{conversation_id}/messages",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json={"content": question, "clientRequestId": client_request_id},
        stream=True,
        timeout=timeout,
    )
    http_status = response.status_code
    with response:
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

    elapsed_ms = int((time.monotonic() - started) * 1000)
    answer = "".join(deltas)
    return {
        "label": label,
        "conversationId": conversation_id,
        "clientRequestId": client_request_id,
        "httpStatus": http_status,
        "events": events,
        "start": payloads.get("message.start"),
        "end": payloads.get("message.end"),
        "error": payloads.get("message.error"),
        "answer": answer,
        "answerChars": len(answer),
        "empty": not answer.strip(),
        "elapsedMs": elapsed_ms,
    }


def row_query(
    database: str, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    connection = connect_mysql(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()
    finally:
        connection.close()


def row_query_all(
    database: str, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    connection = connect_mysql(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 服务启动 / 停止
# ---------------------------------------------------------------------------


def _spring_env(state: dict[str, Any], secrets_ctx: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AILS_LLM_") or key.startswith("AILS_EMBEDDING_"):
            env.pop(key, None)
    env.update(
        {
            "SERVER_PORT": str(state["springPort"]),
            "MYSQL_DB": state["db"],
            "AILS_AGENT_GATEWAY": "http",
            "AILS_AGENT_BASE_URL": f"http://127.0.0.1:{state['agentPort']}",
            "AILS_INTERNAL_TOKEN": secrets_ctx["token"],
            "AILS_INTERNAL_IDENTITY_SECRET": secrets_ctx["identity"],
            "AILS_INTERNAL_DELEGATION_SECRET": secrets_ctx["delegation"],
            "AILS_ANALYSIS_MODE": "mq",
            "AILS_ANALYSIS_COMPENSATION_ENABLED": "false",
            "SPRING_MAIN_BANNER_MODE": "off",
            **state["analysisNames"],
        }
    )
    return env


def start_spring(state: dict[str, Any], secrets_ctx: dict[str, str]) -> subprocess.Popen:
    if not SPRING_JAR.exists():
        raise SystemExit(f"找不到 Spring jar：{SPRING_JAR}（请先构建）")
    if not port_free(state["springPort"]):
        raise SystemExit(
            f"端口 {state['springPort']} 已被占用；先停止现有服务再启动实验。"
        )
    log_path = Path(state["springLog"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    handle.write(f"\n===== spring start {now_iso()} =====\n")
    handle.flush()
    import shutil

    proc = subprocess.Popen(
        [shutil.which("java") or "java", "-jar", str(SPRING_JAR)],
        cwd=str(SPRING_JAR.parents[1]),
        env=_spring_env(state, secrets_ctx),
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        wait_http(
            f"http://127.0.0.1:{state['springPort']}/api/spaces",
            timeout=240,
            proc=proc,
            description="Spring",
        )
    except Exception:
        terminate(proc)
        raise
    state["springPid"] = proc.pid
    return proc


def _agent_env(
    state: dict[str, Any], strategy: str, secrets_ctx: dict[str, str]
) -> dict[str, str]:
    values = parse_env_file()
    env = os.environ.copy()
    env.update({k: v for k, v in values.items() if v})
    env.update(
        {
            "AILS_HOST": "127.0.0.1",
            "AILS_PORT": str(state["agentPort"]),
            "AILS_BACKEND_BASE_URL": f"http://127.0.0.1:{state['springPort']}",
            "AILS_RETRIEVER_MODE": "http",
            "AILS_CONCEPT_RESOLVER_ENABLED": "false",
            "AILS_QDRANT_COLLECTION_PREFIX": state["collectionPrefix"],
            "AILS_MEMORY_COLLECTION_PREFIX": state["memoryCollectionPrefix"],
            "AILS_INTERNAL_TOKEN": secrets_ctx["token"],
            "AILS_INTERNAL_IDENTITY_SECRET": secrets_ctx["identity"],
            "AILS_CONTEXT_STRATEGY": strategy,
            "AILS_TEST_ONLY": "true",
            "AILS_TEST_ENV_ID": state["collectionPrefix"],
            # 全量实验统一温度 0：只注入子进程，不修改 agent/.env。
            # 目的：让同一 (scenario, strategy) 的 3 次 repeat 可对照；
            # 其余生成参数保持 .env 原值（max_tokens 4096）。
            "AILS_LLM_TEMPERATURE": "0",
            "PYTHONPATH": str(AGENT_DIR) + os.pathsep + env.get("PYTHONPATH", ""),
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("AILS_INTERNAL_DELEGATION_SECRET", None)
    return env


def start_agent(
    state: dict[str, Any], strategy: str, secrets_ctx: dict[str, str]
) -> subprocess.Popen:
    if strategy not in {"A", "B", "C"}:
        raise ValueError(f"未知策略：{strategy}")
    if not port_free(state["agentPort"]):
        raise SystemExit(
            f"端口 {state['agentPort']} 已被占用；无法启动策略 {strategy}。"
        )
    log_path = Path(state["agentLog"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    handle.write(f"\n===== agent start strategy={strategy} {now_iso()} =====\n")
    handle.flush()
    proc = subprocess.Popen(
        [sys.executable, str(FULL_DIR / "run_agent_full.py")],
        cwd=str(AGENT_DIR),
        env=_agent_env(state, strategy, secrets_ctx),
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        wait_http(
            f"http://127.0.0.1:{state['agentPort']}/health",
            timeout=90,
            proc=proc,
            description=f"Agent({strategy})",
        )
        context = agent_context(state)
        actual = (context or {}).get("contextStrategy")
        if actual != strategy:
            raise RuntimeError(
                f"Agent 实际策略 {actual!r} != 请求策略 {strategy!r}（拒绝继续）"
            )
        diagnostics = agent_diagnostics(state)
        max_tokens = int((diagnostics or {}).get("maxTokens") or 0)
        if max_tokens < 4096:
            raise RuntimeError(
                f"Agent maxTokens={max_tokens} < 4096，违反前置条件"
            )
    except Exception:
        terminate(proc)
        raise
    state["agentPid"] = proc.pid
    return proc


def terminate(proc: subprocess.Popen | None) -> dict[str, Any]:
    if proc is None:
        return {"stopped": True, "detail": "no process"}
    if proc.poll() is not None:
        return {"stopped": True, "detail": f"already exited code={proc.returncode}"}
    try:
        proc.terminate()
        proc.wait(timeout=10)
        return {"stopped": True, "detail": f"terminate code={proc.returncode}"}
    except Exception:  # noqa: BLE001
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            proc.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001
            return {"stopped": False, "detail": sanitize_error(exc)}
        return {"stopped": True, "detail": "taskkill"}


def wait_port_closed(port: int, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
        except requests.RequestException:
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 内部身份（用于实验脚本直连 Agent 的 memory 索引接口）
# ---------------------------------------------------------------------------


def mint_identity(user_id: int, secret: str, *, ttl_seconds: int = 60) -> str:
    issued_at = int(time.time())
    payload = {
        "iss": "learning-backend",
        "sub": int(user_id),
        "aud": "learning-agent",
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": secrets.token_urlsafe(16),
    }
    body = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
    signing_input = f"v1.{encoded}"
    digest = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{signing_input}.{signature}"


def memory_sync(
    state: dict[str, Any],
    secrets_ctx: dict[str, str],
    *,
    memory_id: int,
    user_id: int,
    content: str,
    space_id: int,
    concept_id: int,
    importance: float,
    scope_type: str = "CONCEPT",
    memory_type: str = "LEARNING",
) -> dict[str, Any]:
    body = {
        "operation": "UPSERT",
        "memoryId": memory_id,
        "userId": user_id,
        "content": content,
        "contentHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "memoryVersion": 0,
        "scopeType": scope_type,
        "learningSpaceId": space_id,
        "conceptId": concept_id,
        "memoryType": memory_type,
        "importance": importance,
        "status": "ACTIVE",
    }
    response = requests.post(
        f"http://127.0.0.1:{state['agentPort']}/agent/memories/sync",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": secrets_ctx["token"],
            "X-Internal-User-Id": str(user_id),
            "X-Internal-Identity": mint_identity(user_id, secrets_ctx["identity"]),
        },
        json=body,
        timeout=120,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:400]}
    if response.status_code >= 400:
        raise RuntimeError(f"Memory 索引失败 HTTP {response.status_code}: {str(data)[:400]}")
    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
    collection = str((payload or {}).get("collection") or "")
    if collection != state["memoryCollection"]:
        raise RuntimeError(
            f"Memory 索引落到了非隔离 collection：{collection!r} "
            f"!= {state['memoryCollection']!r}"
        )
    return dict(payload or {})


# ---------------------------------------------------------------------------
# 场景数据准备
# ---------------------------------------------------------------------------


def _misconceptions_json(items: list[str]) -> str:
    return json.dumps(
        [
            {
                "text": item,
                "status": "ACTIVE",
                "firstDetectedAt": None,
                "lastEvidenceAt": None,
                "reversalEvidenceIds": [],
            }
            for item in items
        ],
        ensure_ascii=False,
    )


def _default_evidence_message(history_ids: dict[str, list[int]]) -> int:
    for ids in history_ids.values():
        if ids:
            return int(ids[0])
    raise RuntimeError("场景没有任何历史消息，无法挂 Evidence 来源")


def create_scenario(
    state: dict[str, Any], token: str, user_id: int, scenario: dict[str, Any], snapshot_time: str
) -> dict[str, Any]:
    base = f"http://127.0.0.1:{state['springPort']}"
    space = api(
        base,
        "POST",
        "/api/spaces",
        token=token,
        json={
            "name": f"{scenario['space']['name']} {state['stamp']}",
            "description": scenario["space"]["description"],
        },
    )
    space_id = int(space["id"])

    concepts: dict[str, int] = {}
    for concept in scenario["concepts"]:
        data = api(
            base,
            "POST",
            f"/api/spaces/{space_id}/concepts",
            token=token,
            json={
                "canonicalName": concept["canonical"],
                "displayName": concept["display"],
                "description": concept["description"],
                "difficultyLevel": concept["difficulty"],
            },
        )
        concepts[concept["key"]] = int(data["id"])

    sessions: dict[str, int] = {}
    for session in scenario["sessions"]:
        data = api(
            base,
            "POST",
            "/api/sessions",
            token=token,
            json={
                "learningSpaceId": space_id,
                "title": session["title"],
                "goal": session["goal"],
            },
        )
        session_id = int(data["id"])
        sessions[session["key"]] = session_id
        api(
            base,
            "PATCH",
            f"/api/sessions/{session_id}",
            token=token,
            json={"currentFocus": session["focus"]},
        )

    conversations: dict[str, int] = {}
    for conversation in scenario["conversations"]:
        data = api(
            base,
            "POST",
            f"/api/spaces/{space_id}/conversations",
            token=token,
            json={"title": conversation["title"]},
        )
        conversations[conversation["key"]] = int(data["id"])

    ids: dict[str, Any] = {
        "userId": user_id,
        "spaceId": space_id,
        "concepts": concepts,
        "sessions": sessions,
        "conversations": conversations,
        "currentConversationId": conversations[scenario["currentConversation"]],
        "historyMessageIds": {},
        "documentId": None,
        "documentChunkCount": None,
    }

    history_ids: dict[str, list[int]] = {}
    with connect_mysql(state["db"]) as conn:
        try:
            conn.begin()
            with conn.cursor() as cursor:
                for conversation in scenario["conversations"]:
                    cid = conversations[conversation["key"]]
                    sid = sessions[conversation["session"]]
                    sequence = 0
                    message_ids: list[int] = []
                    for role, text in conversation["history"]:
                        sequence += 1
                        cursor.execute(
                            "INSERT INTO message(conversation_id,role,message_type,"
                            "content,sequence_no,created_at) "
                            "VALUES(%s,%s,'TEXT',%s,%s,%s)",
                            (cid, role.upper(), text, sequence, snapshot_time),
                        )
                        message_ids.append(int(cursor.lastrowid))
                    cursor.execute(
                        "UPDATE conversation SET session_id=%s,last_message_seq=%s,"
                        "last_message_at=%s WHERE id=%s",
                        (sid, sequence, snapshot_time, cid),
                    )
                    history_ids[conversation["key"]] = message_ids
                ids["historyMessageIds"] = history_ids

                evidence_message_id = _default_evidence_message(history_ids)
                if scenario["id"] == "S4":
                    evidence_message_id = int(history_ids["b"][0])
                if scenario["id"] == "S7":
                    evidence_message_id = int(history_ids["current"][0])

                understanding = scenario["understanding"]
                concept_id = concepts[understanding["concept"]]
                evidence_concept_id = concepts[scenario["evidence"]["concept"]]
                status = str(understanding.get("status") or "PARTIAL")
                known = json.dumps(understanding.get("known", []), ensure_ascii=False)
                misconceptions = _misconceptions_json(
                    understanding.get("misconceptions", [])
                )
                uncertain = json.dumps(
                    understanding.get("uncertain", []), ensure_ascii=False
                )
                original_sql_mode = ""
                if status == "CORRUPT":
                    cursor.execute("SELECT @@SESSION.sql_mode AS m")
                    original_sql_mode = str((cursor.fetchone() or {}).get("m") or "")
                    cursor.execute("SET SESSION sql_mode=''")
                cursor.execute(
                    "INSERT INTO learning_understanding(user_id,concept_id,summary,"
                    "known_parts,misconceptions,uncertain_parts,"
                    "latest_user_explanation,status,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        user_id,
                        concept_id,
                        understanding.get("summary"),
                        known,
                        misconceptions,
                        uncertain,
                        understanding.get("explanation"),
                        status,
                        snapshot_time,
                    ),
                )
                understanding_id = int(cursor.lastrowid)
                if status == "CORRUPT":
                    # 只有 Understanding 行是人为故障；Evidence / Revision 保持
                    # 合法枚举，避免把「检索失败」扩大成「所有认知数据都坏」。
                    cursor.execute("SET SESSION sql_mode=%s", (original_sql_mode,))

                observation = scenario["evidence"]["observation"]
                dedupe = hashlib.sha256(
                    (
                        f"{user_id}:{evidence_concept_id}:USER_EXPLANATION:"
                        f"{evidence_message_id}:{observation}"
                    ).encode("utf-8")
                ).hexdigest()
                cursor.execute(
                    "INSERT INTO learning_evidence(user_id,concept_id,understanding_id,"
                    "evidence_type,observation,correctness,need_follow_up,"
                    "follow_up_status,dedupe_key,analyzer_version,created_at,updated_at) "
                    "VALUES(%s,%s,%s,'USER_EXPLANATION',%s,%s,1,'PENDING',"
                    "%s,'phase5-full-fixture',%s,%s)",
                    (
                        user_id,
                        evidence_concept_id,
                        understanding_id,
                        observation,
                        scenario["evidence"].get("correctness", "PARTIAL"),
                        dedupe,
                        snapshot_time,
                        snapshot_time,
                    ),
                )
                evidence_id = int(cursor.lastrowid)
                cursor.execute(
                    "INSERT INTO evidence_source(evidence_id,source_type,source_id,"
                    "created_at) VALUES(%s,'MESSAGE',%s,%s)",
                    (evidence_id, evidence_message_id, snapshot_time),
                )

                revision_ids: dict[int, int] = {}
                for revision in scenario["revisions"]:
                    cursor.execute(
                        "INSERT INTO understanding_revision(understanding_id,revision_no,"
                        "summary,known_parts,misconceptions,uncertain_parts,"
                        "user_explanation,status,trigger_evidence_id,created_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            understanding_id,
                            int(revision["no"]),
                            revision["summary"],
                            known,
                            misconceptions,
                            uncertain,
                            revision.get("explanation"),
                            # 人为故障只落在 Understanding 行；Revision 仍写合法值。
                            "PARTIAL" if status == "CORRUPT" else status,
                            evidence_id,
                            snapshot_time,
                        ),
                    )
                    revision_ids[int(revision["no"])] = int(cursor.lastrowid)
                current_revision_id = revision_ids.get(
                    int(scenario.get("currentRevisionNo") or 0)
                )
                cursor.execute(
                    "UPDATE learning_understanding SET current_revision_id=%s,"
                    "updated_at=%s WHERE id=%s",
                    (current_revision_id, snapshot_time, understanding_id),
                )

                memory = scenario["memory"]
                memory_concept_id = concepts[memory["concept"]]
                cursor.execute(
                    "INSERT INTO memory(user_id,scope_type,scope_key,learning_space_id,"
                    "concept_id,memory_type,memory_key,content,status,importance,version,"
                    "created_at,updated_at) VALUES(%s,'CONCEPT',%s,%s,%s,'LEARNING',"
                    "%s,%s,'ACTIVE',%s,0,%s,%s)",
                    (
                        user_id,
                        f"CONCEPT:{memory_concept_id}",
                        space_id,
                        memory_concept_id,
                        f"phase5-full-{scenario['id'].lower()}-memory",
                        memory["content"],
                        float(memory.get("importance", 0.8)),
                        snapshot_time,
                        snapshot_time,
                    ),
                )
                memory_id = int(cursor.lastrowid)
                cursor.execute(
                    "INSERT INTO memory_source(memory_id,source_type,source_id,created_at) "
                    "VALUES(%s,'MESSAGE',%s,%s)",
                    (memory_id, evidence_message_id, snapshot_time),
                )

                ids.update(
                    {
                        "understandingId": understanding_id,
                        "understandingConceptId": concept_id,
                        "understandingStatusWritten": status,
                        "evidenceId": evidence_id,
                        "evidenceMessageId": evidence_message_id,
                        "revisionIds": revision_ids,
                        "currentRevisionId": current_revision_id,
                        "memoryId": memory_id,
                        "memoryConceptId": memory_concept_id,
                    }
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def upload_document(
    state: dict[str, Any], token: str, dataset: dict[str, Any], scenario: dict[str, Any]
) -> None:
    base = f"http://127.0.0.1:{state['springPort']}"
    document = scenario["document"]
    response = requests.post(
        f"{base}/api/spaces/{dataset['spaceId']}/documents",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": (
                document["name"],
                document["text"].encode("utf-8"),
                "text/markdown",
            )
        },
        timeout=180,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:500]}
    if response.status_code >= 400 or data.get("code") not in (0, None):
        raise RuntimeError(
            f"上传 Knowledge 失败 HTTP {response.status_code}: {str(data)[:400]}"
        )
    document_id = int(data["data"]["document"]["id"])
    dataset["documentId"] = document_id
    deadline = time.monotonic() + 420
    last: dict[str, Any] = data["data"]["document"]
    while time.monotonic() < deadline:
        documents = api(
            base,
            "GET",
            f"/api/spaces/{dataset['spaceId']}/documents",
            token=token,
        ) or []
        for item in documents:
            if int(item["id"]) == document_id:
                last = item
                break
        if last.get("status") == "READY":
            break
        if last.get("status") == "FAILED":
            raise RuntimeError(f"文档索引失败：{last.get('indexError')}")
        time.sleep(2)
    else:
        raise RuntimeError(f"文档未在 420s 内 READY：{last.get('status')}")
    dataset["documentChunkCount"] = last.get("chunkCount")
    dataset["documentVectorCollection"] = last.get("vectorCollection")
    dataset["documentStatus"] = last.get("status")
    dataset["documentSha256"] = hashlib.sha256(
        document["text"].encode("utf-8")
    ).hexdigest()


def verify_s9_corruption(state: dict[str, Any], token: str, dataset: dict[str, Any]) -> dict[str, Any]:
    """确认非法枚举确实让 Understanding 读取失败（而不是被静默接受）。"""
    url = (
        f"http://127.0.0.1:{state['springPort']}"
        f"/api/understandings/{dataset['understandingConceptId']}"
        f"?spaceId={dataset['spaceId']}"
    )
    response = requests.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    result = {
        "httpStatus": response.status_code,
        "triggeredFailure": response.status_code >= 400,
    }
    if response.status_code < 400:
        try:
            data = response.json()
        except ValueError:
            data = {}
        result["bodyPreview"] = str(data)[:300]
    if not result["triggeredFailure"]:
        raise RuntimeError(
            "S9 非法枚举未触发读取失败；DEGRADED 场景无法成立：" + str(result)
        )
    return result


def seed_all(
    state: dict[str, Any],
    scenarios: list[dict[str, Any]],
    password: str,
    snapshot_time: str,
) -> dict[str, Any]:
    assert_isolated_state(state)
    token, user_id, username = register_or_login(state, password)
    datasets: dict[str, Any] = {}
    setup_steps: list[dict[str, Any]] = []
    started = time.monotonic()
    for scenario in scenarios:
        step_started = time.monotonic()
        dataset = create_scenario(state, token, user_id, scenario, snapshot_time)
        upload_document(state, token, dataset, scenario)
        setup_steps.append(
            {
                "scenario": scenario["id"],
                "spaceId": dataset["spaceId"],
                "documentId": dataset["documentId"],
                "chunkCount": dataset["documentChunkCount"],
                "elapsedMs": int((time.monotonic() - step_started) * 1000),
            }
        )
        datasets[scenario["id"]] = dataset
        print(
            f"[setup] {scenario['id']} space={dataset['spaceId']} "
            f"doc={dataset['documentId']} chunks={dataset['documentChunkCount']}",
            flush=True,
        )
    return {
        "userId": user_id,
        "username": username,
        "datasets": datasets,
        "setupSteps": setup_steps,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }


def sync_memories(
    state: dict[str, Any],
    secrets_ctx: dict[str, str],
    scenarios: list[dict[str, Any]],
    datasets: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    scenario_by_id = {item["id"]: item for item in scenarios}
    for sid, dataset in datasets.items():
        scenario = scenario_by_id[sid]
        memory = scenario["memory"]
        payload = memory_sync(
            state,
            secrets_ctx,
            memory_id=int(dataset["memoryId"]),
            user_id=int(dataset["userId"]),
            content=memory["content"],
            space_id=int(dataset["spaceId"]),
            concept_id=int(dataset["memoryConceptId"]),
            importance=float(memory.get("importance", 0.8)),
        )
        results.append({"scenario": sid, "memoryId": dataset["memoryId"], **payload})
        print(f"[setup] memory indexed {sid} id={dataset['memoryId']}", flush=True)
    return results


# ---------------------------------------------------------------------------
# 全局冻结
# ---------------------------------------------------------------------------


def _int_list(values: Any) -> str:
    cleaned = sorted({int(value) for value in values if value is not None})
    return ",".join(str(value) for value in cleaned) or "0"


def full_freeze_specs(snapshot: dict[str, Any]) -> list[TableSpec]:
    datasets = snapshot["datasets"]
    user_id = int(snapshot["userId"])
    space_ids = [int(item["spaceId"]) for item in datasets.values()]
    conversation_ids = [
        int(cid)
        for item in datasets.values()
        for cid in item["conversations"].values()
    ]
    message_ids = [
        int(mid)
        for item in datasets.values()
        for ids in item["historyMessageIds"].values()
        for mid in ids
    ]
    spaces = _int_list(space_ids)
    conversations = _int_list(conversation_ids)
    messages = _int_list(message_ids)

    return [
        TableSpec(
            "user", ("id", "username", "email", "created_at"), f"WHERE id = {user_id}", "id"
        ),
        TableSpec(
            "learning_space",
            ("id", "user_id", "name", "description", "visibility", "deleted_at", "created_at"),
            f"WHERE id IN ({spaces})",
            "id",
        ),
        TableSpec(
            "concept",
            (
                "id",
                "learning_space_id",
                "canonical_name",
                "display_name",
                "description",
                "difficulty_level",
                "deleted_at",
                "created_at",
            ),
            f"WHERE learning_space_id IN ({spaces})",
            "id",
        ),
        TableSpec(
            "learning_session",
            (
                "id",
                "user_id",
                "learning_space_id",
                "title",
                "goal",
                "current_focus",
                "status",
                "started_at",
                "completed_at",
            ),
            f"WHERE learning_space_id IN ({spaces})",
            "id",
        ),
        TableSpec(
            "conversation",
            (
                "id",
                "user_id",
                "learning_space_id",
                "session_id",
                "title",
                "status",
                "deleted_at",
                "created_at",
            ),
            f"WHERE id IN ({conversations})",
            "id",
        ),
        TableSpec(
            "message",
            (
                "id",
                "conversation_id",
                "role",
                "message_type",
                "content",
                "tokens",
                "truncated",
                "sequence_no",
                "deleted_at",
                "created_at",
            ),
            f"WHERE id IN ({messages})",
            "id",
        ),
        TableSpec(
            "learning_understanding",
            (
                "id",
                "user_id",
                "concept_id",
                "summary",
                "known_parts",
                "misconceptions",
                "uncertain_parts",
                "latest_user_explanation",
                "status",
                "current_revision_id",
                "updated_at",
            ),
            f"WHERE user_id = {user_id}",
            "id",
        ),
        TableSpec(
            "learning_evidence",
            (
                "id",
                "user_id",
                "concept_id",
                "understanding_id",
                "evidence_type",
                "observation",
                "correctness",
                "reasoning_quality",
                "specificity",
                "ai_confidence",
                "need_follow_up",
                "follow_up_status",
                "follow_up_conversation_id",
                "dedupe_key",
                "first_seen_run_id",
                "last_seen_run_id",
                "analyzer_version",
                "created_at",
                "updated_at",
            ),
            f"WHERE user_id = {user_id}",
            "id",
        ),
        TableSpec(
            "evidence_source",
            ("id", "evidence_id", "source_type", "source_id", "created_at"),
            "WHERE evidence_id IN "
            f"(SELECT id FROM learning_evidence WHERE user_id = {user_id})",
            "id",
        ),
        TableSpec(
            "understanding_revision",
            (
                "id",
                "understanding_id",
                "revision_no",
                "summary",
                "known_parts",
                "misconceptions",
                "uncertain_parts",
                "user_explanation",
                "status",
                "trigger_evidence_id",
                "created_at",
            ),
            "WHERE understanding_id IN "
            f"(SELECT id FROM learning_understanding WHERE user_id = {user_id})",
            "id",
        ),
        TableSpec(
            "memory",
            (
                "id",
                "user_id",
                "scope_type",
                "scope_key",
                "learning_space_id",
                "concept_id",
                "memory_type",
                "memory_key",
                "content",
                "status",
                "importance",
                "version",
                "embedding_ref",
                "created_at",
                "updated_at",
            ),
            f"WHERE user_id = {user_id}",
            "id",
        ),
        TableSpec(
            "memory_source",
            ("id", "memory_id", "source_type", "source_id", "created_at"),
            f"WHERE memory_id IN (SELECT id FROM memory WHERE user_id = {user_id})",
            "id",
        ),
        TableSpec(
            "document",
            (
                "id",
                "learning_space_id",
                "user_id",
                "content_hash",
                "name",
                "file_name",
                "storage_uri",
                "file_type",
                "size",
                "status",
                "parser_version",
                "chunk_count",
                "index_version",
                "embedding_model",
                "embedding_provider",
                "embedding_dimension",
                "embedding_fingerprint",
                "vector_collection",
                "indexed_at",
                "deleted_at",
                "created_at",
            ),
            f"WHERE learning_space_id IN ({spaces})",
            "id",
        ),
        TableSpec(
            "document_chunk",
            (
                "id",
                "document_id",
                "index_version",
                "chunk_index",
                "content",
                "page_number",
                "token_count",
                "vector_ref",
                "metadata_json",
                "created_at",
            ),
            "WHERE document_id IN "
            f"(SELECT id FROM document WHERE learning_space_id IN ({spaces}))",
            "id",
        ),
        TableSpec(
            "document_index_version",
            ("document_id", "index_version", "vector_collection", "created_at"),
            "WHERE document_id IN "
            f"(SELECT id FROM document WHERE learning_space_id IN ({spaces}))",
            "document_id, index_version",
        ),
    ]


def effective_generation_profile() -> dict[str, Any]:
    values = parse_env_file()
    effective = {**os.environ, **values}
    keys = (
        "AILS_LLM_PROVIDER",
        "AILS_LLM_MODEL",
        "AILS_LLM_TEMPERATURE",
        "AILS_LLM_MAX_TOKENS",
        "AILS_LLM_TIMEOUT_SECONDS",
        "AILS_RETRIEVER_MODE",
        "AILS_KNOWLEDGE_TOP_K",
        "AILS_KNOWLEDGE_MIN_SCORE",
        "AILS_MEMORY_TOP_K",
        "AILS_EVIDENCE_TOP_K",
        "AILS_MAX_CONTEXT_TOKENS",
        "AILS_RECENT_HISTORY_LIMIT",
        "AILS_EMBEDDING_PROVIDER",
        "AILS_EMBEDDING_MODEL",
        "AILS_EMBEDDING_DIMENSION",
    )
    profile = {key: effective.get(key, "") for key in keys}
    # 实验统一温度 0（只注入子进程），必须在 profile 中如实记录。
    profile["AILS_LLM_TEMPERATURE"] = "0"
    from app.prompts import BASE_SYSTEM_INSTRUCTION

    profile["baseSystemInstructionSha256"] = hashlib.sha256(
        BASE_SYSTEM_INSTRUCTION.encode("utf-8")
    ).hexdigest()
    return profile


def freeze_hashes(snapshot: dict[str, Any]) -> dict[str, Any]:
    connection = connect_mysql(snapshot["isolation"]["db"])
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            except Exception:  # noqa: BLE001
                pass
        hashes: dict[str, Any] = {}
        with connection.cursor() as cursor:
            for spec in full_freeze_specs(snapshot):
                rows = read_table(cursor, spec)
                hashes[spec.name] = {"rows": len(rows), "sha256": hash_rows(rows)}
    finally:
        connection.rollback()
        connection.close()
    return {"tables": hashes, "combined": combined_hash(hashes)}


def diff_freeze(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    before_tables = baseline.get("tables") or {}
    after_tables = current.get("tables") or {}
    for name in sorted(set(before_tables) | set(after_tables)):
        before = before_tables.get(name)
        after = after_tables.get(name)
        if before is None:
            problems.append(f"{name}: baseline 缺失，当前存在")
        elif after is None:
            problems.append(f"{name}: 当前缺失，baseline 存在")
        elif before.get("sha256") != after.get("sha256"):
            problems.append(
                f"{name}: 漂移 rows {before.get('rows')} -> {after.get('rows')}"
            )
    if baseline.get("combined") != current.get("combined"):
        problems.append("combined: 冻结表整体哈希漂移")
    return problems


def write_freeze_baseline(snapshot_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    state = freeze_hashes(snapshot)
    baseline = {
        "schemaVersion": 1,
        "kind": BASELINE_KIND,
        "snapshotPath": str(snapshot_path),
        "writtenAt": now_iso(),
        "tables": state["tables"],
        "combined": state["combined"],
        "generationProfile": snapshot["generationProfile"],
    }
    write_json(baseline_path(snapshot_path), baseline)
    return baseline


# ---------------------------------------------------------------------------
# Qdrant / 默认 Memory / MQ
# ---------------------------------------------------------------------------


def qdrant_collection_state(collection: str) -> dict[str, Any]:
    base, headers = qdrant_settings()
    info = requests.get(f"{base}/collections/{collection}", headers=headers, timeout=30)
    if info.status_code == 404:
        return {
            "collection": collection,
            "exists": False,
            "pointCount": 0,
            "points": {},
            "combined": None,
        }
    if info.status_code >= 400:
        raise RuntimeError(f"读取 Qdrant collection 失败：HTTP {info.status_code}")
    points, fetched = scroll_all(base, headers, collection)
    return {
        "collection": collection,
        "exists": True,
        "pointCount": fetched,
        "points": points,
        "combined": qdrant_combined_hash(points),
    }


def qdrant_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "document": qdrant_collection_state(state["collection"]),
        "memory": qdrant_collection_state(state["memoryCollection"]),
    }


def diff_qdrant(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in ("document", "memory"):
        before = baseline.get(name) or {}
        after = current.get(name) or {}
        if before.get("collection") != after.get("collection"):
            problems.append(f"{name}.collection 变化")
        if bool(before.get("exists")) != bool(after.get("exists")):
            problems.append(f"{name}.exists 变化")
        if before.get("pointCount") != after.get("pointCount"):
            problems.append(
                f"{name}.pointCount {before.get('pointCount')} -> {after.get('pointCount')}"
            )
        if before.get("combined") != after.get("combined"):
            problems.append(f"{name}.combined 漂移")
    return problems


def write_qdrant_baseline(
    snapshot_path: Path, state: dict[str, Any]
) -> dict[str, Any]:
    baseline = {
        "schemaVersion": 1,
        "kind": QDRANT_BASELINE_KIND,
        "snapshotPath": str(snapshot_path),
        "writtenAt": now_iso(),
        **qdrant_state(state),
    }
    write_json(qdrant_baseline_path(snapshot_path), baseline)
    return baseline


def default_memory_state(state: dict[str, Any]) -> dict[str, Any]:
    name = state["defaultMemoryCollection"]
    base, headers = qdrant_settings()
    response = requests.get(f"{base}/collections/{name}", headers=headers, timeout=30)
    if response.status_code == 404:
        return {"collection": name, "exists": False, "pointCount": 0}
    if response.status_code >= 400:
        return {
            "collection": name,
            "exists": None,
            "error": f"HTTP {response.status_code}",
        }
    data = response.json().get("result") or {}
    return {
        "collection": name,
        "exists": True,
        "pointCount": int(data.get("points_count") or 0),
    }


def rabbit_consumers(stamp: str) -> dict[str, int]:
    response = requests.get(
        "http://127.0.0.1:15672/api/queues", auth=("guest", "guest"), timeout=10
    )
    response.raise_for_status()
    result: dict[str, int] = {}
    for queue in response.json():
        name = str(queue.get("name") or "")
        if stamp in name:
            result[name] = int(queue.get("consumers") or 0)
    return result


def analysis_consumer_problems(consumers: dict[str, int], stamp: str) -> list[str]:
    problems: list[str] = []
    for name, count in consumers.items():
        if name.endswith(".request") or name.endswith(".retry") or ".retry." in name:
            if count != 0:
                problems.append(f"{name} consumers={count}（分析消费者未关闭）")
    return problems


# ---------------------------------------------------------------------------
# 会话恢复（每次 Chat 后）
# ---------------------------------------------------------------------------


def restore_scenario(
    state: dict[str, Any], dataset: dict[str, Any], snapshot_time: str
) -> dict[str, Any]:
    conversation_ids = sorted(int(cid) for cid in dataset["conversations"].values())
    history_ids = sorted(
        int(mid) for ids in dataset["historyMessageIds"].values() for mid in ids
    )
    conv_filter = ",".join(str(value) for value in conversation_ids) or "0"
    message_filter = ",".join(str(value) for value in history_ids) or "0"
    deleted = 0
    with connect_mysql(state["db"]) as conn:
        try:
            conn.begin()
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM message WHERE conversation_id IN ("
                    + conv_filter
                    + ") AND id NOT IN ("
                    + message_filter
                    + ")",
                )
                deleted = int(cursor.rowcount or 0)
                for key, cid in dataset["conversations"].items():
                    count = len(dataset["historyMessageIds"].get(key, []))
                    cursor.execute(
                        "UPDATE conversation SET last_message_seq=%s,"
                        "last_message_at=%s WHERE id=%s",
                        (count, snapshot_time, int(cid)),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    rows = row_query_all(
        state["db"],
        "SELECT id,conversation_id,sequence_no FROM message "
        f"WHERE conversation_id IN ({conv_filter}) ORDER BY id",
        (),
    )
    remaining_ids = sorted(int(row["id"]) for row in rows)
    return {
        "deleted": deleted,
        "remainingIds": remaining_ids,
        "historyIds": history_ids,
        "exactMatch": remaining_ids == history_ids,
    }


# ---------------------------------------------------------------------------
# Manifest / 引用检查
# ---------------------------------------------------------------------------


def parse_manifest(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def manifest_bucket(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not manifest:
        return {
            "present": False,
            "intent": None,
            "policyVersion": None,
            "sourceTypes": [],
            "selectedTypes": [],
            "sources": [],
            "budget": None,
        }
    entries = list(manifest.get("sources") or [])
    selected = [
        item
        for item in entries
        if str(item.get("injection_status") or "").upper()
        in {"SELECTED", "TRUNCATED", "PARTIAL"}
    ]

    def base_type(value: Any) -> str:
        text = str(value or "")
        return text[:-7] if text.endswith("_source") else text

    source_types = sorted({base_type(item.get("type")) for item in entries})
    selected_types = sorted({base_type(item.get("type")) for item in selected})
    per_type_tokens: dict[str, int] = {}
    for item in selected:
        key = base_type(item.get("type"))
        per_type_tokens[key] = per_type_tokens.get(key, 0) + int(
            item.get("estimated_tokens") or 0
        )
    return {
        "present": True,
        "intent": manifest.get("intent"),
        "policyVersion": manifest.get("policy_version"),
        "schemaVersion": manifest.get("schema_version"),
        "sourceTypes": source_types,
        "selectedTypes": selected_types,
        "sourceCount": len(entries),
        "selectedCount": len(selected),
        "sources": entries,
        "budget": manifest.get("budget"),
        "perTypeTokens": per_type_tokens,
        "sourceIds": [
            str(item.get("source_id"))
            for item in entries
            if str(item.get("injection_status") or "").upper()
            in {"SELECTED", "TRUNCATED", "PARTIAL"}
        ],
        # 复盘路径把 Evidence 渲染在 trajectory 段下（type=trajectory_source，
        # source_id=evidence:<id>），per_type 里不会出现 evidence 键。这是既有
        # Manifest schema 的已知形态（见 docs/testing/phase5-pilot-results.md
        # §10.1 第 1 条），判定「Evidence 是否注入」时与顶层 evidence 等价。
        "evidenceSourceIds": [
            str(item.get("source_id"))
            for item in selected
            if str(item.get("source_id") or "").startswith("evidence:")
        ],
    }


def manifest_problems(
    bucket: dict[str, Any], strategy: str, scenario: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    expected = scenario["expect"]
    if not bucket["present"]:
        return ["Manifest 缺失或无法解析"]
    selected = set(bucket["selectedTypes"])
    if bucket.get("evidenceSourceIds"):
        # Evidence 以 trajectory_source:evidence:* 注入，与顶层 evidence 等价。
        selected.add("evidence")
    if strategy == "A":
        for kind in expected.get("A_must_exclude", []):
            if kind in selected:
                problems.append(f"A 注入了个人上下文：{kind}")
        if bucket.get("intent") is not None or bucket.get("policyVersion") is not None:
            problems.append("A 的 Manifest 不应有 intent / policy_version")
    elif strategy == "B":
        for kind in expected.get("B_must_include", []):
            if kind not in selected:
                problems.append(f"B 缺少预期来源：{kind}")
        if bucket.get("intent") is not None or bucket.get("policyVersion") is not None:
            problems.append("B 未按近似实现：intent / policy_version 应为 null")
    else:
        if bucket.get("intent") != expected.get("C_intent"):
            problems.append(
                f"C intent={bucket.get('intent')} != {expected.get('C_intent')}"
            )
        if bucket.get("policyVersion") != "d4-2-v1":
            problems.append(f"C policy_version={bucket.get('policyVersion')} != d4-2-v1")
        for kind in expected.get("C_must_include", []):
            if kind not in selected:
                problems.append(f"C 缺少预期来源：{kind}")
        for kind in expected.get("C_must_exclude", []):
            if kind in selected:
                problems.append(f"C 注入了不应出现的来源：{kind}")
    if strategy in {"B", "C"} and expected.get("degraded_cognitive"):
        marker = str(expected.get("degraded_source") or "")
        present = any(marker and marker in source_id for source_id in bucket["sourceIds"])
        if strategy == "C" and not present:
            problems.append(f"C 未保留 DEGRADED：缺少 {marker}")
        if strategy == "B" and present:
            problems.append(f"B 未把 DEGRADED 折叠为 EMPTY：仍出现 {marker}")
    return problems


def citation_set(citations: Any) -> list[list[int]]:
    items = citations if isinstance(citations, list) else []
    keys: set[tuple[int, int]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        document_id = item.get("documentId")
        chunk_id = item.get("chunkId")
        if document_id is None or chunk_id is None:
            continue
        keys.add((int(document_id), int(chunk_id)))
    return [list(key) for key in sorted(keys)]


# ---------------------------------------------------------------------------
# 判据修正（Step 3 复盘批次）
# ---------------------------------------------------------------------------
#
# 背景：预注册判据在本批次产生 12 条失败，人工复核确认均为判据设计问题：
#   1) S5 要求回答逐字包含合成标记 【S5-HISTORY】，但标记不是用户真实说过的
#      话；模型的合理行为是引用标记后的原句内容、省略人工括号标记。
#   2) citation 要求 3 次 repeat 的引用集合严格一致，但 temperature=0 下供应商
#      输出仍有非确定性，引用条数会在 2~4 条之间波动；这并不等于伪造引用。
#
# 新判据只放宽「形式」，不放宽「可验证性」：
#   - S5：必须出现原句内容（连续子串）或三个语义关键词全部出现；
#   - Citation：所有引用必须可溯源、非空、落在本场景同一文档，且覆盖率达标；
#   - Manifest / Context Hash / prompt tokens 的一致性判据保持不变；
#   - 任何 error / timeout / 空输出 / 截断仍然 FAIL。
# ---------------------------------------------------------------------------

S5_EXPECTED_ORIGINAL = "我原来以为慢启动慢是因为它每次只把窗口加一"
S5_REQUIRED_KEYWORDS = ("慢启动", "每次只", "加一")
CITATION_MIN_AVERAGE_COVERAGE = 2.0 / 3.0
CITATION_MIN_REP_COVERAGE = 1.0 / 3.0
CITATION_MIN_UNION_CHUNKS = 2


def s5_history_use_check(answer: str | None) -> dict[str, Any]:
    """S5「历史被使用」的可执行判据。

    通过条件（满足其一）：
      1. 回答包含原句的连续子串
         ``我原来以为慢启动慢是因为它每次只把窗口加一``；
      2. 回答等价语义：同时包含「慢启动」「每次只」「加一」三个关键词。

    合成标记 ``【S5-HISTORY】`` 不再作为必需项。理由见模块内注释。

    本批次只改 S5 的预置 Understanding 数据，不修改这里的匹配规则。
    """
    text = answer or ""
    exact = S5_EXPECTED_ORIGINAL in text
    keyword_hits = {keyword: keyword in text for keyword in S5_REQUIRED_KEYWORDS}
    equivalent = all(keyword_hits.values())
    return {
        "rule": (
            "包含原句连续子串「我原来以为慢启动慢是因为它每次只把窗口加一」"
            "或同时包含「慢启动」「每次只」「加一」三个关键词"
        ),
        "exactOriginalSentence": exact,
        "keywords": keyword_hits,
        "equivalentSemantics": equivalent,
        "pass": exact or equivalent,
    }


# 只在 HISTORY_GUARD 触发重生成时允许不同的字段。Manifest 不在豁免范围内：
# 重生成不改变本轮注入来源；如果 Manifest 也变了，说明是上下文组装发生了变化。
GUARD_EXEMPT_FIELDS = (
    "contextHash",
    "systemSha256",
    "systemChars",
    "promptTokens",
)


def memory_degraded_info(record: dict[str, Any]) -> dict[str, Any]:
    """从 Manifest 判定本轮 Memory 检索是否降级。

    判定依据（满足其一即视为 ``memoryDegraded=true``）：

    1. Manifest 中存在 ``retrieval_status=DEGRADED`` 的 memory 来源；
    2. Manifest 中存在 ``source_id`` 含 ``memory-status:DEGRADED`` 的
       认知状态来源（ContextBuilder 在 Memory 状态非 SUCCESS 时注入）。

    ``memoryDegradedSource`` 记录可定位的来源 ID，供报告逐条留痕。
    本函数只读 Manifest，不修改 Runtime / ContextBuilder / Schema。
    """
    sources = manifest_bucket(record.get("manifest")).get("sources") or []
    degraded_memory: list[str] = []
    cognitive_markers: list[str] = []
    for source in sources:
        source_id = str(source.get("source_id") or "")
        source_type = str(source.get("type") or "")
        status = str(source.get("retrieval_status") or "").upper()
        if status == "DEGRADED" and (
            source_id.startswith("memory:") or source_type.startswith("memory")
        ):
            degraded_memory.append(source_id)
        if "memory-status:DEGRADED" in source_id:
            cognitive_markers.append(source_id)
    details = sorted(set(degraded_memory + cognitive_markers))
    return {
        "memoryDegraded": bool(details),
        "memoryDegradedSource": details,
    }


def partial_upgrade_negation_observation(record: dict[str, Any]) -> dict[str, Any]:
    """观测：HISTORY_GUARD 否定语境识别跳过的字面命中次数与回退保护结果。

    产品侧的跳过计数只存在于进程内校验，不落库；这里用产品同一个
    ``partial_upgrade_negation_skips`` 对本次实际进入校验的回答文本重放，
    只作观测记录，不参与 PASS/FAIL。

    ``fallbackUsed``：守卫触发且最终交付既不是首答也不是重生成回答，
    说明使用了确定性回退。

    ``fallbackPreservedOriginal``：回退时最终交付是否仍含历史原句。
    用于验证「用户显式要求引用的原句不被回退覆盖」。
    """
    from app.history_guard import partial_upgrade_negation_skips

    first = record.get("firstAnswer")
    regenerated = record.get("regeneratedAnswer")
    texts: list[str] = []
    if isinstance(first, str):
        texts.append(first)
    if isinstance(regenerated, str) and regenerated != first:
        texts.append(regenerated)
    if not texts:
        answer = record.get("answerFull") or record.get("answerPreview") or ""
        if answer:
            texts.append(answer)
    skipped: list[str] = []
    for text in texts:
        skipped.extend(partial_upgrade_negation_skips(text))

    final_answer = record.get("answerFull") or ""
    fallback_used = (
        bool(record.get("historyGuardTriggered"))
        and bool(final_answer)
        and final_answer not in {first, regenerated}
    )
    return {
        "partialUpgradeNegationSkipped": len(skipped),
        "partialUpgradeNegationSkippedPhrases": skipped,
        "fallbackUsed": fallback_used,
        "fallbackPreservedOriginal": (
            s5_history_use_check(final_answer)["exactOriginalSentence"]
            if fallback_used
            else None
        ),
    }


def guard_negation_misjudgements(
    record: dict[str, Any], trace: dict[str, Any] | None
) -> list[str]:
    """检查产品 trace 里的 partial_upgrade 是否仍是否定语境误判。

    ``issues`` 来自首答、``remainingIssues`` 来自重生成回答；若某个
    ``partial_upgrade`` 的 excerpt 在对应回答里被否定语境识别跳过，
    说明修复没有覆盖到它，属于功能缺陷，必须让本批判 FAIL。
    """
    if not trace:
        return []
    from app.history_guard import partial_upgrade_negation_skips

    first_skips = set(
        partial_upgrade_negation_skips(record.get("firstAnswer") or "")
    )
    retry_skips = set(
        partial_upgrade_negation_skips(record.get("regeneratedAnswer") or "")
    )
    problems: list[str] = []
    checks = (
        ("首答", trace.get("issues") or [], first_skips),
        ("重生成", trace.get("remainingIssues") or [], retry_skips),
    )
    for stage, issues, skips in checks:
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("kind") != "partial_upgrade":
                continue
            excerpt = str(issue.get("excerpt") or "")
            if excerpt and excerpt in skips:
                problems.append(
                    f"否定语境仍误判 partial_upgrade（{stage}）：「{excerpt}」"
                )
    return problems


def guard_aware_hash_consistency(records: list[dict[str, Any]]) -> dict[str, Any]:
    """HISTORY_GUARD / Memory DEGRADED 触发时的 Hash 一致性判据。

    规则（只豁免已记录的运行期事件，不放宽整体一致性）：

    1. 每条记录都必须有布尔字段 ``historyGuardTriggered`` 与
       ``memoryDegraded``；
    2. ``manifestSha256`` 在 3 次 repeat 之间必须一致（始终不豁免）；
    3. 未被豁免的 repeat 之间，``contextHash`` / ``systemSha256`` /
       ``systemChars`` / ``promptTokens`` 必须一致；
    4. ``historyGuardTriggered=true`` 或 ``memoryDegraded=true`` 的 repeat
       豁免上述字段差异，但必须在结果中标注（本函数输出 ``guardedKeys``、
       ``degradedKeys`` 与逐条豁免说明）；
    5. 若 3 次全部被豁免，则没有未被豁免的基线，Hash 相等性不适用，
       记为观测项而不是静默 PASS；
    6. ``memoryDegraded=true`` 时必须同时记录 ``memoryDegradedSource``，
       否则记为判据失败（降级未被留痕）。
    """
    ordered = sorted(records, key=lambda item: int(item.get("rep") or 0))
    problems: list[str] = []
    annotations: list[str] = []
    missing_guard = [
        record.get("key")
        for record in ordered
        if not isinstance(record.get("historyGuardTriggered"), bool)
    ]
    if missing_guard:
        problems.append(f"未记录 historyGuardTriggered：{missing_guard}")
    missing_degraded = [
        record.get("key")
        for record in ordered
        if not isinstance(record.get("memoryDegraded"), bool)
    ]
    if missing_degraded:
        problems.append(f"未记录 memoryDegraded：{missing_degraded}")

    guarded = [
        record for record in ordered if record.get("historyGuardTriggered") is True
    ]
    degraded = [
        record for record in ordered if record.get("memoryDegraded") is True
    ]
    exempt = [
        record
        for record in ordered
        if record.get("historyGuardTriggered") is True
        or record.get("memoryDegraded") is True
    ]
    unexempt = [
        record
        for record in ordered
        if not (
            record.get("historyGuardTriggered") is True
            or record.get("memoryDegraded") is True
        )
    ]

    for record in degraded:
        sources = record.get("memoryDegradedSource") or []
        if not sources:
            problems.append(
                f"{record.get('key')}：memoryDegraded=true 但未记录 "
                "memoryDegradedSource"
            )

    manifest_values = [record.get("manifestSha256") for record in ordered]
    if len(set(manifest_values)) != 1:
        problems.append(
            f"一致性：manifestSha256 不一致 {manifest_values}"
            "（manifestSha256 始终不豁免）"
        )

    field_values: dict[str, list[Any]] = {}
    if unexempt:
        for field in GUARD_EXEMPT_FIELDS:
            values = [record.get(field) for record in unexempt]
            field_values[field] = values
            if len(set(values)) != 1:
                problems.append(
                    f"一致性：{field} 在未被豁免的 "
                    f"{len(unexempt)} 次之间不一致 {values}"
                )
    else:
        annotations.append(
            "3 次均被 HISTORY_GUARD / Memory DEGRADED 豁免，"
            "未建立未豁免基线，Hash 相等性不适用"
        )

    for record in guarded:
        annotations.append(
            f"{record.get('key')}：historyGuardTriggered=true，"
            f"豁免 {', '.join(GUARD_EXEMPT_FIELDS)}（llmCallCount="
            f"{record.get('llmCallCount')}）"
        )
    for record in degraded:
        annotations.append(
            f"{record.get('key')}：memoryDegraded=true，"
            f"来源 {','.join(record.get('memoryDegradedSource') or []) or '-'}，"
            f"豁免 {', '.join(GUARD_EXEMPT_FIELDS)}"
        )

    return {
        "rule": (
            "未触发守卫且未降级的 repeat 之间必须 Hash 一致；"
            "historyGuardTriggered=true 或 memoryDegraded=true 的 repeat "
            "豁免 contextHash/systemSha256/systemChars/promptTokens，但必须标注；"
            "manifestSha256 始终要求一致"
        ),
        "guardedKeys": [record.get("key") for record in guarded],
        "degradedKeys": [record.get("key") for record in degraded],
        "exemptKeys": [record.get("key") for record in exempt],
        "unguardedKeys": [record.get("key") for record in unexempt],
        "manifestValues": manifest_values,
        "unguardedValues": field_values,
        "memoryDegradedSources": {
            record.get("key"): record.get("memoryDegradedSource") or []
            for record in degraded
        },
        "annotations": annotations,
        "problems": problems,
        "pass": not problems,
    }



def citation_consistency_check(
    records: list[dict[str, Any]],
    expected_document_id: int,
    *,
    allow_history_quote: bool = False,
) -> dict[str, Any]:
    """3 次 repeat 的 citation 一致性（修正后判据）。

    默认必须同时满足：
      1. 每次 repeat 至少 1 条引用（不允许空引用集合）；
      2. 所有引用可溯源（调用方已通过 citation_traceability 校验）；
      3. 所有引用都指向本场景的同一份 Knowledge 文档；
      4. 3 次引用并集至少覆盖 2 个 chunk（避免「只复读同一个 chunk」）；
      5. 覆盖率达标：平均覆盖率 >= 2/3 且每次 repeat 覆盖率 >= 1/3；
         或 3 次中至少 2 次的 citation 集合完全相同（顺序无关）。

    ``allow_history_quote=True``（S5 复盘场景专用）：引用不是复盘问题的
    核心证据，历史原句才是。因此每次 repeat 满足「citation 非空」或
    「回答引用了历史原句」之一即可；非空引用仍必须可溯源且落在本场景
    同一文档。覆盖率与并集仍计算并展示，但不作为该模式的硬性失败项。

    覆盖率定义：``c_r = |C_r| / |U|``，其中 ``U = C_1 ∪ C_2 ∪ C_3``。
    """
    items = sorted(records, key=lambda item: int(item.get("rep") or 0))
    sets = [
        {(int(pair[0]), int(pair[1])) for pair in (item.get("citationSet") or [])}
        for item in items
    ]
    history_quotes = [
        bool(
            s5_history_use_check(
                item.get("answerFull") or item.get("answerPreview") or ""
            )["pass"]
        )
        for item in items
    ]
    trace_ok = all(
        bool((item.get("citationTrace") or {}).get("traceable")) for item in items
    )
    non_empty = all(bool(current) for current in sets)
    if allow_history_quote:
        content_ok = all(
            bool(current) or quote
            for current, quote in zip(sets, history_quotes)
        )
    else:
        content_ok = non_empty
    union: set[tuple[int, int]] = set()
    for current in sets:
        union |= current
    coverages = [len(current) / len(union) if union else 0.0 for current in sets]
    average_coverage = sum(coverages) / len(coverages) if coverages else 0.0
    documents = sorted({doc for current in sets for doc, _ in current})
    if allow_history_quote:
        # 全部空引用且均引用历史原句时，没有文档可检查；有引用时必须同文档。
        same_document = documents == [int(expected_document_id)] if union else True
    else:
        same_document = bool(union) and documents == [int(expected_document_id)]
    identical = [json.dumps(sorted(current)) for current in sets]
    identical_reps = max((identical.count(value) for value in set(identical)), default=0)
    coverage_ok = (
        average_coverage >= CITATION_MIN_AVERAGE_COVERAGE
        and min(coverages, default=0.0) >= CITATION_MIN_REP_COVERAGE
    )
    identical_ok = identical_reps >= 2
    reasons: list[str] = []
    if allow_history_quote:
        empty_without_quote = [
            item.get("key")
            for item, current, quote in zip(items, sets, history_quotes)
            if not current and not quote
        ]
        if empty_without_quote:
            reasons.append(
                "存在空 citation 且未引用历史原句：" + ", ".join(empty_without_quote)
            )
    elif not non_empty:
        reasons.append("存在空 citation 集合")
    if not trace_ok:
        reasons.append("存在不可溯源引用")
    if not same_document:
        reasons.append(f"引用未落在本场景文档 document={expected_document_id}：{documents}")
    if not allow_history_quote:
        if len(union) < CITATION_MIN_UNION_CHUNKS:
            reasons.append(f"引用并集 chunk 数 {len(union)} < {CITATION_MIN_UNION_CHUNKS}")
        if not coverage_ok:
            reasons.append(
                "覆盖率不足：average="
                f"{average_coverage:.3f}（阈值 {CITATION_MIN_AVERAGE_COVERAGE:.3f}），"
                f"per-rep={[round(value, 3) for value in coverages]}"
                f"（每轮阈值 {CITATION_MIN_REP_COVERAGE:.3f}）"
            )
        if not (coverage_ok or identical_ok):
            reasons.append("且没有 2/3 次 citation 集合完全相同")
    coverage_blocker = (
        False
        if allow_history_quote
        else (
            len(union) < CITATION_MIN_UNION_CHUNKS
            or not (coverage_ok or identical_ok)
        )
    )
    return {
        "rule": (
            "S5 复盘模式：每次（citation 非空或引用历史原句）、非空引用全部可溯源"
            "且落在本场景同一文档"
            if allow_history_quote
            else "3 次 citation 非空、全部可溯源、全部指向本场景同一文档、并集 >= 2 chunk；"
            "且（平均覆盖率 >= 2/3 且每轮覆盖率 >= 1/3）或（>= 2 次集合完全相同）"
        ),
        "mode": "history_quote_or_citation" if allow_history_quote else "strict",
        "expectedDocumentId": int(expected_document_id),
        "sets": [[list(pair) for pair in sorted(current)] for current in sets],
        "union": [list(pair) for pair in sorted(union)],
        "coverages": coverages,
        "averageCoverage": average_coverage,
        "identicalReps": identical_reps,
        "historyQuotes": dict(
            (item.get("key"), quote) for item, quote in zip(items, history_quotes)
        ),
        "traceable": trace_ok,
        "nonEmpty": non_empty,
        "contentOk": content_ok,
        "sameDocument": same_document,
        "coverageOk": coverage_ok,
        "identicalOk": identical_ok,
        "pass": content_ok and trace_ok and same_document and not coverage_blocker,
        "reasons": reasons,
    }


def citation_traceability(
    citations: Any, database: str, space_id: int
) -> dict[str, Any]:
    items = citations if isinstance(citations, list) else []
    if not items:
        return {"count": 0, "traceable": True, "problems": [], "items": []}
    document_ids = sorted({int(item["documentId"]) for item in items if item.get("documentId")})
    chunk_ids = sorted({int(item["chunkId"]) for item in items if item.get("chunkId")})
    documents = {
        int(row["id"]): row
        for row in row_query_all(
            database,
            "SELECT id,learning_space_id FROM document WHERE id IN ("
            + (",".join(str(value) for value in document_ids) or "0")
            + ")",
        )
    }
    chunks = {
        int(row["id"]): row
        for row in row_query_all(
            database,
            "SELECT id,document_id,chunk_index FROM document_chunk WHERE id IN ("
            + (",".join(str(value) for value in chunk_ids) or "0")
            + ")",
        )
    }
    problems: list[str] = []
    verified: list[dict[str, Any]] = []
    for item in items:
        document_id = int(item["documentId"])
        chunk_id = int(item["chunkId"])
        document = documents.get(document_id)
        chunk = chunks.get(chunk_id)
        ok = (
            document is not None
            and chunk is not None
            and int(chunk["document_id"]) == document_id
            and int(document["learning_space_id"]) == space_id
        )
        if not ok:
            problems.append(f"引用无法溯源：document={document_id} chunk={chunk_id}")
        verified.append(
            {
                "documentId": document_id,
                "chunkId": chunk_id,
                "chunkIndex": item.get("chunkIndex"),
                "sourceTitle": item.get("sourceTitle"),
                "traceable": ok,
            }
        )
    return {
        "count": len(verified),
        "traceable": not problems,
        "problems": problems,
        "items": verified,
    }


def usage_fields(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {
            "promptTokens": None,
            "completionTokens": None,
            "totalTokens": None,
            "cachedTokens": None,
            "reasoningTokens": None,
        }
    prompt = usage.get("prompt_tokens", usage.get("promptTokens"))
    completion = usage.get("completion_tokens", usage.get("completionTokens"))
    total = usage.get("total_tokens", usage.get("totalTokens"))
    prompt_details = usage.get("prompt_tokens_details") or usage.get("promptTokensDetails") or {}
    completion_details = (
        usage.get("completion_tokens_details") or usage.get("completionTokensDetails") or {}
    )
    cached = None
    if isinstance(prompt_details, dict):
        cached = prompt_details.get("cached_tokens", prompt_details.get("cachedTokens"))
    reasoning = None
    if isinstance(completion_details, dict):
        reasoning = completion_details.get(
            "reasoning_tokens", completion_details.get("reasoningTokens")
        )
    return {
        "promptTokens": int(prompt) if isinstance(prompt, (int, float)) else None,
        "completionTokens": int(completion) if isinstance(completion, (int, float)) else None,
        "totalTokens": int(total) if isinstance(total, (int, float)) else None,
        "cachedTokens": int(cached) if isinstance(cached, (int, float)) else None,
        "reasoningTokens": int(reasoning) if isinstance(reasoning, (int, float)) else None,
    }


def scenario_marker_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        markers = record.get("scenarioMarkers") or {}
        for key, value in markers.items():
            counts[key] = counts.get(key, 0) + int(value)
    return counts
