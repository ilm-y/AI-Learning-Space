"""Phase 5 Pilot：启动隔离的 Agent(8001) + Spring(8081)。

设计约束：
- 不修改 agent/.env、application-local.yml 或任何生产配置；
- 只在子进程环境里注入本次 Pilot 的临时参数与临时签名密钥；
- 状态文件只记录端口/PID/库名/collection/日志路径，不记录密钥；
- 使用当前源码新构建的 backend jar（构建由外部执行）。

用法（仓库根目录）：
    python scripts/phase5/pilot/start_services.py
输出最后一行 STATE_FILE=<path>。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    AGENT_DIR,
    AGENT_ENV,
    LOCAL_YML,
    SPRING_JAR,
    parse_env_file,
    sanitize_error,
    write_json,
)

AGENT_PORT = 8001
SPRING_PORT = 8081


def _analysis_names(stamp: str) -> dict[str, str]:
    base = f"phase5.pilot.{stamp}"
    suffix = stamp.replace("-", "_")
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
        # 只为避免未使用变量告警；路由键里已包含 stamp。
        "_suffix": suffix,
    }


def _tail_sanitized(path: Path, secrets_to_hide: list[str], lines: int = 30) -> str:
    if not path.exists():
        return "<日志不存在>"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    joined = "\n".join(text)
    for secret in secrets_to_hide:
        if secret:
            joined = joined.replace(secret, "***")
    return joined


def _wait_http(url: str, *, timeout: float, proc: subprocess.Popen, description: str) -> None:
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


def main() -> int:
    if not SPRING_JAR.exists():
        raise SystemExit(f"找不到 Spring jar：{SPRING_JAR}（请先构建）")
    env_values = parse_env_file(AGENT_ENV)
    token = env_values.get("AILS_INTERNAL_TOKEN", "").strip()
    if len(token) < 16:
        raise SystemExit("agent/.env 的 AILS_INTERNAL_TOKEN 为空或过短，前置条件 FAIL")

    # 校验 local yml 里的 token 与 .env 一致；不一致说明配置漂移。
    local_text = LOCAL_YML.read_text(encoding="utf-8") if LOCAL_YML.exists() else ""
    local_token = re.search(r"^\s*token:\s*\"([^\"]+)\"", local_text, re.M)
    if local_token and local_token.group(1).strip() != token:
        raise SystemExit("agent/.env 与 application-local.yml 的 internal token 不一致，前置条件 FAIL")
    if not local_token:
        print("WARN: application-local.yml 未找到具名 internal token；将由环境变量覆盖为 .env 值", flush=True)

    stamp = time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    db_name = "phase5_pilot_" + stamp.replace("-", "_")
    collection_prefix = "phase5-pilot-" + stamp

    identity_secret = secrets.token_hex(32)
    delegation_secret = secrets.token_hex(32)
    if len({token, identity_secret, delegation_secret}) != 3:
        raise SystemExit("内部凭据生成异常：token/identity/delegation 必须互不相同")

    temp_dir = Path(tempfile.mkdtemp(prefix="phase5-pilot-"))
    agent_log = temp_dir / "agent.log"
    spring_log = temp_dir / "spring.log"

    agent_env = os.environ.copy()
    agent_env.update({k: v for k, v in env_values.items() if v})
    agent_env.update(
        {
            "AILS_HOST": "127.0.0.1",
            "AILS_PORT": str(AGENT_PORT),
            "AILS_BACKEND_BASE_URL": f"http://127.0.0.1:{SPRING_PORT}",
            "AILS_RETRIEVER_MODE": "http",
            "AILS_CONCEPT_RESOLVER_ENABLED": "false",
            "AILS_QDRANT_COLLECTION_PREFIX": collection_prefix,
            "AILS_INTERNAL_TOKEN": token,
            "AILS_INTERNAL_IDENTITY_SECRET": identity_secret,
            "PYTHONPATH": str(AGENT_DIR) + os.pathsep + agent_env.get("PYTHONPATH", ""),
            "PYTHONUNBUFFERED": "1",
        }
    )

    analysis = _analysis_names(stamp)
    analysis.pop("_suffix", None)
    spring_env = os.environ.copy()
    spring_env.update(
        {
            "SERVER_PORT": str(SPRING_PORT),
            "MYSQL_DB": db_name,
            "AILS_AGENT_GATEWAY": "http",
            "AILS_AGENT_BASE_URL": f"http://127.0.0.1:{AGENT_PORT}",
            "AILS_INTERNAL_TOKEN": token,
            "AILS_INTERNAL_IDENTITY_SECRET": identity_secret,
            "AILS_INTERNAL_DELEGATION_SECRET": delegation_secret,
            "AILS_ANALYSIS_MODE": "mq",
            "AILS_ANALYSIS_COMPENSATION_ENABLED": "false",
            "SPRING_MAIN_BANNER_MODE": "off",
            **analysis,
        }
    )
    # Spring 不需要 Agent 的模型/Embedding Key；只继承本机环境与内部凭据。
    for key in list(spring_env):
        if key.startswith("AILS_LLM_") or key.startswith("AILS_EMBEDDING_"):
            spring_env.pop(key, None)

    agent_log_handle = agent_log.open("w", encoding="utf-8", errors="replace")
    spring_log_handle = spring_log.open("w", encoding="utf-8", errors="replace")

    agent_proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("run_agent.py"))],
        cwd=str(AGENT_DIR),
        env=agent_env,
        stdout=agent_log_handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    spring_proc = subprocess.Popen(
        [shutil.which("java") or "java", "-jar", str(SPRING_JAR)],
        cwd=str(SPRING_JAR.parents[1]),
        env=spring_env,
        stdout=spring_log_handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    secrets_to_hide = [token, identity_secret, delegation_secret]
    try:
        _wait_http(f"http://127.0.0.1:{AGENT_PORT}/health", timeout=60, proc=agent_proc, description="Agent")
        _wait_http(f"http://127.0.0.1:{SPRING_PORT}/api/spaces", timeout=150, proc=spring_proc, description="Spring")
    except Exception as exc:  # noqa: BLE001
        for proc in (agent_proc, spring_proc):
            if proc.poll() is None:
                proc.terminate()
        print(f"启动失败：{sanitize_error(exc)}", file=sys.stderr)
        print("--- agent.log tail ---", file=sys.stderr)
        print(_tail_sanitized(agent_log, secrets_to_hide), file=sys.stderr)
        print("--- spring.log tail ---", file=sys.stderr)
        print(_tail_sanitized(spring_log, secrets_to_hide), file=sys.stderr)
        return 1

    state = {
        "stamp": stamp,
        "db": db_name,
        "collectionPrefix": collection_prefix,
        "collection": f"{collection_prefix}_text-embedding-v4_1024",
        "agentPort": AGENT_PORT,
        "springPort": SPRING_PORT,
        "agentPid": agent_proc.pid,
        "springPid": spring_proc.pid,
        "tempDir": str(temp_dir),
        "agentLog": str(agent_log),
        "springLog": str(spring_log),
        "analysisNames": analysis,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "credentials": {
            "tokenAligned": True,
            "identitySecretSet": True,
            "delegationSecretSet": True,
            "agentHoldsDelegationSecret": False,
        },
    }
    state_path = temp_dir / "state.json"
    write_json(state_path, state)
    print(json.dumps({k: v for k, v in state.items() if k != "analysisNames"}, ensure_ascii=False))
    print(f"STATE_FILE={state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
