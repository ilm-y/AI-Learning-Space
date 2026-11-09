"""Phase 5 Pilot 共用工具。

只做三件事：
1. 读取现有配置（agent/.env、未跟踪的 application-local.yml），但不打印任何密钥；
2. 连接本机 MySQL；
3. 读写 Pilot 的临时 state/results 文件。

本模块不修改任何生产配置文件，也不把密钥写入任何文件。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = ROOT / "agent"
RESOURCES_DIR = ROOT / "backend" / "backend" / "src" / "main" / "resources"
LOCAL_YML = RESOURCES_DIR / "application-local.yml"
AGENT_ENV = AGENT_DIR / ".env"
SPRING_JAR = ROOT / "backend" / "backend" / "target" / "backend-0.0.1-SNAPSHOT.jar"


def parse_env_file(path: Path = AGENT_ENV) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env；忽略注释与空行。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def local_mysql_credentials() -> dict[str, Any]:
    """从 application-local.yml 读取 datasource 用户名/口令；只返回内存值。"""
    text = LOCAL_YML.read_text(encoding="utf-8")
    # 文件很小，datasource 段内只有一组 username/password。
    username = re.search(r"^\s*username:\s*\"?([^\"\n#]+)\"?\s*$", text, re.M)
    password = re.search(r"^\s*password:\s*\"?([^\"\n#]+)\"?\s*$", text, re.M)
    if not username or not password:
        raise RuntimeError("application-local.yml 中找不到 datasource 凭据")
    return {
        "host": "127.0.0.1",
        "port": 3306,
        "user": username.group(1).strip(),
        "password": password.group(1).strip(),
    }


def connect_mysql(database: str | None = None):
    import pymysql
    from pymysql.cursors import DictCursor

    creds = local_mysql_credentials()
    return pymysql.connect(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        password=creds["password"],
        database=database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )


def read_state(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sanitize_error(exc: BaseException) -> str:
    """只保留异常类型与简短消息；避免把配置/凭据带进文档。"""
    text = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:300]}"
