"""Phase 5 pre-Step3 统一数据快照的共用定义。

三个脚本（setup / freeze / restore）只共享两件事：

1. 同一批固定的数据内容与冻结表定义；
2. 同一套隔离断言——任何写操作都只允许落到 ``phase5_pilot_<stamp>`` 数据库与
   ``phase5-pilot-<stamp>*`` Qdrant collection。

本模块不读取、不打印任何密钥；MySQL 凭据由 ``scripts/phase5/pilot/common.py``
从本机 ``application-local.yml`` 读取，只在内存中使用。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRE_STEP3_DIR = Path(__file__).resolve().parent
PILOT_DIR = PRE_STEP3_DIR.parent / "pilot"
AGENT_DIR = PRE_STEP3_DIR.parents[2] / "agent"
for _path in (PILOT_DIR, AGENT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import connect_mysql, parse_env_file, read_state, sanitize_error, write_json  # noqa: E402

# 三组共用的中性问题：不要求个人历史，A 不会因缺个人上下文被问题本身惩罚；
# 同时含「依据」，让 Evidence 检索在 A/B/C 中都按同一规则触发。
NEUTRAL_QUESTION = (
    "TCP 三次握手中，为什么第三次握手是必要的？"
    "请结合你可用的资料回答，并说明你的依据。"
)

DB_NAME_RE = re.compile(r"^phase5_pilot_\d{14}_[0-9a-f]{6}$")
DOC_PREFIX_RE = re.compile(r"^phase5-pilot-\d{14}-[0-9a-f]{6}$")
MEMORY_PREFIX_RE = re.compile(r"^phase5-pilot-\d{14}-[0-9a-f]{6}-memory$")

FIXTURE_MARKER = "PHASE5-PRESTEP3-SNAPSHOT"
PASSWORD = "phase5-prestep3-only"  # 隔离测试用户口令；不用于任何真实账号

HISTORY_USER = (
    f"【{FIXTURE_MARKER}】我先说说我的理解：TCP 三次握手是客户端发 SYN，"
    "服务端回 SYN-ACK，客户端再回 ACK。"
)
HISTORY_ASSISTANT = (
    f"【{FIXTURE_MARKER}】你抓住了三步顺序，但还没解释第三次 ACK 的必要性。"
)
UNDERSTANDING_SUMMARY = "知道三次握手的顺序，但不确定第三次握手的必要性。"
UNDERSTANDING_EXPLANATION = "客户端先发 SYN，服务端回 SYN-ACK，客户端再回 ACK。"
UNDERSTANDING_KNOWN = ["能说出 SYN、SYN-ACK、ACK 的顺序"]
UNDERSTANDING_UNCERTAIN = ["第三次握手是否只是为了确认服务端收到 ACK"]
EVIDENCE_OBSERVATION = (
    f"【{FIXTURE_MARKER}】用户能按顺序复述三次握手，"
    "但没有解释第三次握手为什么不能省。"
)
MEMORY_CONTENT = (
    f"【{FIXTURE_MARKER}】用户偏好先用报文顺序理解 TCP 三次握手，再补协议细节；"
    "目前对第三次握手的必要性有疑问。"
)
SESSION_GOAL = "真正理解 TCP 三次握手，尤其是第三次 ACK 的必要性"
SESSION_FOCUS = "说不清第三次握手为什么不能省"

DOCUMENT_TEXT = f"""# {FIXTURE_MARKER} TCP 三次握手

第三次握手（ACK）用于确认服务端此前发出的 SYN-ACK 已被客户端收到。
它让服务端确认客户端的接收与发送能力都正常，并避免历史重复连接请求
被误认为新连接。

## 为什么第三次不能省

如果没有第三次握手，服务端无法确认自己发出的 SYN-ACK 是否被客户端收到，
一个延迟到达的旧 SYN 可能让服务端建立一条客户端并不需要的连接。
"""

# 冻结表定义。columns 是参与哈希的列；where 是只读查询过滤条件。
# 刻意排除运行期可变字段：conversation 的游标、session 的 last_active_at、
# document 的索引重试/对账状态等。它们不是快照内容，不应导致误报漂移。
@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    where: str
    order_by: str

    @property
    def select_sql(self) -> str:
        cols = ", ".join(f"`{column}`" for column in self.columns)
        return (
            f"SELECT {cols} FROM `{self.name}` "
            f"{self.where} ORDER BY {self.order_by}"
        )


def freeze_tables(manifest: dict[str, Any]) -> list[TableSpec]:
    """按快照清单里的 ID 生成冻结表查询。

    所有条件都来自 setup_snapshot.py 写入的整数 ID，不使用用户输入拼接 SQL。
    """
    dataset = manifest["dataset"]
    user_id = int(dataset["userId"])
    space_id = int(dataset["spaceId"])

    # 兼容两种表示：单会话（本批次）与 pilot 的多会话结构（历史数据）。
    if "conversationId" in dataset:
        conversations = [int(dataset["conversationId"])]
    else:
        conversations = [int(value) for value in dataset["conversationIds"].values()]
    history = dataset["historyMessageIds"]
    if isinstance(history, dict):
        message_ids = [int(value) for values in history.values() for value in values]
    else:
        message_ids = [int(value) for value in history]

    conv_filter = ",".join(str(value) for value in conversations) or "0"
    message_filter = ",".join(str(value) for value in message_ids) or "0"

    return [
        TableSpec(
            "user",
            ("id", "username", "email", "created_at"),
            f"WHERE id = {user_id}",
            "id",
        ),
        TableSpec(
            "learning_space",
            (
                "id",
                "user_id",
                "name",
                "description",
                "visibility",
                "deleted_at",
                "created_at",
            ),
            f"WHERE id = {space_id}",
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
            f"WHERE learning_space_id = {space_id}",
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
            f"WHERE learning_space_id = {space_id}",
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
            f"WHERE id IN ({conv_filter})",
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
            f"WHERE id IN ({message_filter})",
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
            f"WHERE learning_space_id = {space_id}",
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
            f"(SELECT id FROM document WHERE learning_space_id = {space_id})",
            "id",
        ),
        TableSpec(
            "document_index_version",
            ("document_id", "index_version", "vector_collection", "created_at"),
            "WHERE document_id IN "
            f"(SELECT id FROM document WHERE learning_space_id = {space_id})",
            "document_id, index_version",
        ),
    ]


def assert_isolated_state(state: dict[str, Any]) -> str:
    """任何读写前先证明 state 指向隔离环境；返回 env id（doc collection 前缀）。"""
    db = str(state.get("db") or "")
    doc_prefix = str(state.get("collectionPrefix") or "")
    memory_prefix = str(state.get("memoryCollectionPrefix") or "")
    problems: list[str] = []
    if not DB_NAME_RE.fullmatch(db):
        problems.append("db 不是 phase5_pilot_<stamp> 隔离库")
    if not DOC_PREFIX_RE.fullmatch(doc_prefix):
        problems.append("collectionPrefix 不是 phase5-pilot-<stamp>")
    if not MEMORY_PREFIX_RE.fullmatch(memory_prefix):
        problems.append("memoryCollectionPrefix 不是 phase5-pilot-<stamp>-memory")
    if doc_prefix and memory_prefix != f"{doc_prefix}-memory":
        problems.append("memoryCollectionPrefix 与 collectionPrefix 不匹配")
    if problems:
        raise SystemExit("隔离断言失败，拒绝执行：" + "；".join(problems))
    return doc_prefix


def generation_profile() -> dict[str, Any]:
    """记录三组必须一致的生成参数与 System Instruction 指纹。

    只读取 agent/.env 中与生成有关的非密钥项；不读取、不输出 API key。
    该 profile 写入 snapshot.json，freeze_check.py 在每组运行前重新计算并对比。
    """
    env = parse_env_file(AGENT_DIR / ".env")
    keys = (
        # 生成
        "AILS_LLM_PROVIDER",
        "AILS_LLM_MODEL",
        "AILS_LLM_TEMPERATURE",
        "AILS_LLM_MAX_TOKENS",
        "AILS_LLM_TIMEOUT_SECONDS",
        # 检索预算
        "AILS_RETRIEVER_MODE",
        "AILS_KNOWLEDGE_TOP_K",
        "AILS_KNOWLEDGE_MIN_SCORE",
        "AILS_MEMORY_TOP_K",
        "AILS_EVIDENCE_TOP_K",
        "AILS_MAX_CONTEXT_TOKENS",
        "AILS_RECENT_HISTORY_LIMIT",
        # Knowledge 向量可比性（不是密钥）
        "AILS_EMBEDDING_PROVIDER",
        "AILS_EMBEDDING_MODEL",
        "AILS_EMBEDDING_DIMENSION",
    )
    profile: dict[str, Any] = {
        key: env.get(key, "") for key in keys
    }
    from app.prompts import BASE_SYSTEM_INSTRUCTION

    profile["baseSystemInstructionSha256"] = hashlib.sha256(
        BASE_SYSTEM_INSTRUCTION.encode("utf-8")
    ).hexdigest()
    return profile


def canonical_row(row: dict[str, Any]) -> str:
    """稳定序列化一行；datetime / Decimal / bytes 统一成字符串。"""

    def _normalize(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, bytes):
            return value.hex()
        return str(value)

    return json.dumps(
        {key: _normalize(value) for key, value in row.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def hash_rows(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_table(cursor, spec: TableSpec) -> list[dict[str, Any]]:
    cursor.execute(spec.select_sql)
    return list(cursor.fetchall())


def snapshot_hashes(connection, manifest: dict[str, Any]) -> dict[str, Any]:
    """只读计算所有冻结表的行数与内容哈希。"""
    result: dict[str, Any] = {}
    with connection.cursor() as cursor:
        for spec in freeze_tables(manifest):
            rows = read_table(cursor, spec)
            result[spec.name] = {"rows": len(rows), "sha256": hash_rows(rows)}
    return result


def combined_hash(hashes: dict[str, Any]) -> str:
    payload = json.dumps(hashes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_state(path: str | Path) -> dict[str, Any]:
    return read_state(path)


__all__ = [
    "AGENT_DIR",
    "NEUTRAL_QUESTION",
    "PASSWORD",
    "DOCUMENT_TEXT",
    "FIXTURE_MARKER",
    "HISTORY_USER",
    "HISTORY_ASSISTANT",
    "UNDERSTANDING_SUMMARY",
    "UNDERSTANDING_EXPLANATION",
    "UNDERSTANDING_KNOWN",
    "UNDERSTANDING_UNCERTAIN",
    "EVIDENCE_OBSERVATION",
    "MEMORY_CONTENT",
    "SESSION_GOAL",
    "SESSION_FOCUS",
    "TableSpec",
    "assert_isolated_state",
    "canonical_row",
    "combined_hash",
    "connect_mysql",
    "freeze_tables",
    "generation_profile",
    "hash_rows",
    "load_state",
    "read_table",
    "sanitize_error",
    "snapshot_hashes",
    "write_json",
]
