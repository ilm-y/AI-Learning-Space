"""Phase 5 Step 2：最小真实连通性验证（不做生产写入）。

约束（来自 Phase 5 任务书）：
- LLM <= 2 次、Embedding <= 2 次、Tavily <= 1 次、Qdrant 写入 <= 2 次、MySQL 写入 <= 2 次；
- 本脚本把每次 provider 调用压到 1 次（重试均为 0 / max_attempts=1）；
- Qdrant 使用 ``phase5-test-`` 前缀的独立 collection，验证后删除；
- MySQL 使用独立数据库 ``ails_phase5_test_<uuid>``，验证后 DROP；
- 不打印任何 API Key / 密码 / 完整 prompt；输出中出现的已知密钥会被替换为 <redacted>。

运行：
    python scripts/phase5/connectivity_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = ROOT / "agent"
sys.path.insert(0, str(AGENT_ROOT))

from app.config import Settings  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.llm import build_llm_adapter  # noqa: E402
from app.llm.base import ChatMessage  # noqa: E402

RESULT: dict[str, object] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def redact(text: object, secrets: list[str]) -> str:
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "<redacted>")
    return value


def safe_error(exc: BaseException, secrets: list[str]) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": redact(str(exc), secrets)[:400],
    }


async def check_llm(settings: Settings, secrets: list[str]) -> dict[str, object]:
    """1 次真实流式调用；max_attempts=1，避免任何隐式重试。"""
    adapter = build_llm_adapter(settings)
    if not hasattr(adapter, "stream_chat_with_completion"):
        await adapter.aclose()
        return {"status": "BLOCKED", "reason": "适配器不支持终态诊断，无法记录 finish_reason"}

    # 不走 build_llm_adapter 的默认 max_attempts=2：按任务书只允许 1 次尝试。
    from app.llm.openai_compatible import OpenAICompatibleAdapter

    if isinstance(adapter, OpenAICompatibleAdapter):
        await adapter.aclose()
        adapter = OpenAICompatibleAdapter(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=0.0,
            max_tokens=16,
            timeout_seconds=settings.llm_timeout_seconds,
            max_attempts=1,
        )

    messages = [ChatMessage(role="user", content="回答一个字：好")]
    started = time.perf_counter()
    chunks: list[str] = []
    completion = None
    try:
        stream = adapter.stream_chat_with_completion(messages, temperature=0.0, max_tokens=16)
        async for delta in stream:
            chunks.append(delta)
        completion = stream.completion
        diagnostics = getattr(adapter, "last_diagnostics", None)
        content = "".join(chunks)
        return {
            "status": "PASS" if content.strip() else "FAIL",
            "provider": settings.llm_provider,
            "adapter": adapter.name,
            "model": settings.llm_model,
            "baseUrlHost": settings.llm_base_url.split("://", 1)[-1].split("/", 1)[0],
            "requestId": getattr(diagnostics, "request_id", None),
            "httpStatus": getattr(diagnostics, "http_status", None),
            "finishReason": getattr(completion, "finish_reason", None),
            "truncated": bool(getattr(completion, "truncated", False)),
            "usage": getattr(diagnostics, "usage", None),
            "attempts": getattr(diagnostics, "attempt", None),
            "emptyReason": getattr(diagnostics, "empty_reason", None),
            "errorCategory": getattr(diagnostics, "error_category", None),
            "latencyMs": ms(started),
            "contentChars": len(content),
            "contentPreview": redact(content[:40], secrets),
            "nonEmpty": bool(content.strip()),
        }
    except Exception as exc:  # noqa: BLE001 - 失败即记录，不重试
        diagnostics = getattr(adapter, "last_diagnostics", None)
        return {
            "status": "FAIL",
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "baseUrlHost": settings.llm_base_url.split("://", 1)[-1].split("/", 1)[0],
            "requestId": getattr(diagnostics, "request_id", None),
            "httpStatus": getattr(diagnostics, "http_status", None),
            "finishReason": getattr(diagnostics, "finish_reason", None),
            "usage": getattr(diagnostics, "usage", None),
            "attempts": getattr(diagnostics, "attempt", None),
            "emptyReason": getattr(diagnostics, "empty_reason", None),
            "errorCategory": getattr(diagnostics, "error_category", None),
            "latencyMs": ms(started),
            "contentChars": len("".join(chunks)),
            "nonEmpty": False,
            "error": safe_error(exc, secrets),
        }
    finally:
        await adapter.aclose()


async def check_embedding(settings: Settings, secrets: list[str]) -> tuple[dict[str, object], list[float] | None]:
    """1 次真实 Embedding 调用；max_retries=0，避免任何隐式重试。"""
    from app.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider

    provider = build_embedding_provider(settings)
    if isinstance(provider, OpenAICompatibleEmbeddingProvider):
        await provider.aclose()
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout_seconds=settings.embedding_timeout_seconds,
            max_retries=0,
            batch_size=settings.embedding_batch_size,
        )

    text = "TCP 三次握手"
    started = time.perf_counter()
    try:
        vectors = await provider.embed([text])
        vector = vectors[0] if vectors else []
        norm = sum(v * v for v in vector) ** 0.5
        result = {
            "status": "PASS" if vector else "FAIL",
            "provider": provider.name,
            "model": provider.model,
            "dimension": len(vector),
            "expectedDimension": provider.dimension,
            "count": len(vectors),
            "nonEmpty": bool(vector),
            "vectorNorm": round(norm, 6),
            "latencyMs": ms(started),
            "fingerprint": provider.fingerprint,
        }
        return result, vector
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "FAIL",
            "provider": provider.name,
            "model": provider.model,
            "expectedDimension": provider.dimension,
            "latencyMs": ms(started),
            "error": safe_error(exc, secrets),
        }, None
    finally:
        await provider.aclose()


async def check_qdrant(
    settings: Settings, vector: list[float] | None, secrets: list[str]
) -> dict[str, object]:
    """隔离 collection：创建 -> 写入 1 点 -> 按向量检索 -> 删除并验证。"""
    base = settings.qdrant_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.qdrant_api_key:
        headers["api-key"] = settings.qdrant_api_key
    collection = f"phase5-test-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    dimension = settings.embedding_dimension
    if vector is None:
        # Embedding 失败时的受控兜底：只验证 Qdrant 写入/检索链路，向量来源明确标注。
        vector = [((i % 17) + 1) / 1000.0 for i in range(dimension)]
        vector_source = "synthetic-fallback (embedding failed)"
    else:
        vector_source = "real embedding result (text-embedding-v4)"

    point_id = str(uuid.uuid4())
    marker = f"phase5-connectivity-{uuid.uuid4().hex[:8]}"
    result: dict[str, object] = {
        "status": "FAIL",
        "urlHost": base.split("://", 1)[-1],
        "collection": collection,
        "dimension": dimension,
        "distance": "Cosine",
        "vectorSource": vector_source,
        "writeCount": 0,
        "queryCount": 0,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            started = time.perf_counter()
            response = await client.put(
                f"{base}/collections/{collection}",
                json={"vectors": {"size": dimension, "distance": "Cosine"}},
                headers=headers,
            )
            result["createLatencyMs"] = ms(started)
            result["createHttpStatus"] = response.status_code
            response.raise_for_status()

            started = time.perf_counter()
            response = await client.put(
                f"{base}/collections/{collection}/points?wait=true",
                json={
                    "points": [
                        {
                            "id": point_id,
                            "vector": vector,
                            "payload": {
                                "phase": "phase5",
                                "marker": marker,
                                "purpose": "connectivity-check",
                                "vector_source": vector_source,
                            },
                        }
                    ]
                },
                headers=headers,
            )
            result["upsertLatencyMs"] = ms(started)
            result["upsertHttpStatus"] = response.status_code
            response.raise_for_status()
            result["writeCount"] = 1

            started = time.perf_counter()
            response = await client.post(
                f"{base}/collections/{collection}/points/search",
                json={"vector": vector, "limit": 1, "with_payload": True},
                headers=headers,
            )
            result["searchLatencyMs"] = ms(started)
            result["searchHttpStatus"] = response.status_code
            response.raise_for_status()
            hits = response.json().get("result", [])
            result["queryCount"] = 1
            result["hitCount"] = len(hits)
            if hits:
                hit = hits[0]
                result["recalled"] = str(hit.get("id")) == point_id
                result["score"] = hit.get("score")
                result["recalledMarker"] = (hit.get("payload") or {}).get("marker")
            else:
                result["recalled"] = False

            result["status"] = (
                "PASS"
                if result.get("recalled") and result.get("recalledMarker") == marker
                else "FAIL"
            )
        except Exception as exc:  # noqa: BLE001
            result["error"] = safe_error(exc, secrets)
        finally:
            # 清理必须执行，且单独记录结果——清理失败不能隐瞒。
            cleanup: dict[str, object] = {}
            try:
                started = time.perf_counter()
                response = await client.delete(f"{base}/collections/{collection}")
                cleanup["deleteLatencyMs"] = ms(started)
                cleanup["deleteHttpStatus"] = response.status_code
                cleanup["deleted"] = response.status_code in (200, 202)
            except Exception as exc:  # noqa: BLE001
                cleanup["deleted"] = False
                cleanup["error"] = safe_error(exc, secrets)
            try:
                response = await client.get(f"{base}/collections/{collection}")
                cleanup["getAfterDeleteHttpStatus"] = response.status_code
                cleanup["verifiedGone"] = response.status_code == 404
            except Exception as exc:  # noqa: BLE001
                cleanup["verifiedGone"] = False
                cleanup["verifyError"] = safe_error(exc, secrets)
            try:
                response = await client.get(f"{base}/collections")
                names = [item.get("name") for item in response.json().get("result", {}).get("collections", [])]
                cleanup["stillListed"] = collection in names
            except Exception as exc:  # noqa: BLE001
                cleanup["stillListed"] = None
                cleanup["listError"] = safe_error(exc, secrets)
            result["cleanup"] = cleanup
    return result


def check_tavily() -> dict[str, object]:
    """Tavily 未集成：不伪造调用，直接标记 BLOCKED。"""
    return {
        "status": "BLOCKED",
        "reason": "Agent 代码中不存在 Tavily 客户端、工具抽象、依赖或配置项；无可调用的集成点。",
        "evidence": [
            "agent/requirements.txt 无 tavily 依赖",
            "agent/app 下无 tavily/tool/function-calling 客户端",
            "agent/.env 无 AILS_TAVILY_* 配置",
        ],
    }


def check_mysql(secrets: list[str]) -> dict[str, object]:
    """隔离数据库：建库 -> 建 3 张探针表 -> 1 个事务写入 3 行 -> 读回 -> DROP。"""
    import pymysql

    local = ROOT / "backend/backend/src/main/resources/application-local.yml"
    config = yaml.safe_load(local.read_text(encoding="utf-8"))["spring"]["datasource"]
    host = "127.0.0.1"
    port = 3306
    user = config.get("username", "root")
    password = config.get("password", "")
    db_name = f"ails_phase5_test_{uuid.uuid4().hex[:12]}"
    result: dict[str, object] = {
        "status": "FAIL",
        "host": f"{host}:{port}",
        "database": db_name,
        "isolation": "dedicated database, dropped after check; production schema untouched",
        "writeCount": 0,
        "readCount": 0,
    }
    conn = None
    try:
        started = time.perf_counter()
        conn = pymysql.connect(
            host=host, port=port, user=user, password=password, connect_timeout=5, autocommit=True
        )
        result["connectLatencyMs"] = ms(started)

        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        result["createDatabaseLatencyMs"] = ms(started)

        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE `{db_name}`.`phase5_user` ("
                "`id` BIGINT PRIMARY KEY, `username` VARCHAR(64) NOT NULL, "
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            cur.execute(
                f"CREATE TABLE `{db_name}`.`phase5_space` ("
                "`id` BIGINT PRIMARY KEY, `user_id` BIGINT NOT NULL, `name` VARCHAR(64) NOT NULL)"
            )
            cur.execute(
                f"CREATE TABLE `{db_name}`.`phase5_message` ("
                "`id` BIGINT PRIMARY KEY, `space_id` BIGINT NOT NULL, "
                "`role` VARCHAR(16) NOT NULL, `content` VARCHAR(200) NOT NULL)"
            )
        result["createTablesLatencyMs"] = ms(started)

        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute("START TRANSACTION")
            cur.execute(
                f"INSERT INTO `{db_name}`.`phase5_user` (`id`, `username`) VALUES (1, 'phase5_probe_user')"
            )
            cur.execute(
                f"INSERT INTO `{db_name}`.`phase5_space` (`id`, `user_id`, `name`) VALUES (1, 1, 'phase5_probe_space')"
            )
            cur.execute(
                f"INSERT INTO `{db_name}`.`phase5_message` (`id`, `space_id`, `role`, `content`) "
                "VALUES (1, 1, 'user', 'phase5 probe message')"
            )
            cur.execute("COMMIT")
        result["writeLatencyMs"] = ms(started)
        result["writeCount"] = 1
        result["writeNote"] = "1 transaction / 3 rows (user, space, message)"

        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT u.username, s.name, m.role, m.content "
                f"FROM `{db_name}`.`phase5_user` u "
                f"JOIN `{db_name}`.`phase5_space` s ON s.user_id = u.id "
                f"JOIN `{db_name}`.`phase5_message` m ON m.space_id = s.id WHERE u.id = 1"
            )
            row = cur.fetchone()
        result["readLatencyMs"] = ms(started)
        result["readCount"] = 1
        result["readBack"] = list(row) if row else None
        result["status"] = "PASS" if row else "FAIL"

        started = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE `{db_name}`")
        result["cleanupLatencyMs"] = ms(started)
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (db_name,))
            result["cleanup"] = {
                "dropped": True,
                "verifiedGone": cur.fetchone() is None,
            }
    except Exception as exc:  # noqa: BLE001
        result["error"] = safe_error(exc, secrets)
        # 清理尽力而为；失败要显式记录，不能隐藏。
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
                    cur.execute("SHOW DATABASES LIKE %s", (db_name,))
                    result["cleanup"] = {
                        "dropped": True,
                        "verifiedGone": cur.fetchone() is None,
                        "afterError": True,
                    }
            except Exception as cleanup_exc:  # noqa: BLE001
                result["cleanup"] = {
                    "dropped": False,
                    "error": safe_error(cleanup_exc, secrets),
                }
    finally:
        if conn is not None:
            conn.close()
    return result


def check_reachability() -> dict[str, object]:
    import socket

    targets = {
        "qdrant": ("127.0.0.1", 6333),
        "spring-boot": ("127.0.0.1", 8080),
        "mysql": ("127.0.0.1", 3306),
        "agent": ("127.0.0.1", 8000),
    }
    out: dict[str, object] = {}
    for name, (host, port) in targets.items():
        sock = socket.socket()
        sock.settimeout(2)
        try:
            sock.connect((host, port))
            out[name] = "OPEN"
        except Exception as exc:  # noqa: BLE001
            out[name] = f"CLOSED ({type(exc).__name__})"
        finally:
            sock.close()
    return out


async def main() -> int:
    settings = Settings()
    secrets = [
        settings.llm_api_key,
        settings.embedding_api_key,
        settings.qdrant_api_key,
        settings.internal_token,
        settings.internal_identity_secret,
    ]
    RESULT["startedAt"] = now_iso()
    RESULT["reachability"] = check_reachability()
    RESULT["effectiveConfig"] = {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_base_url_host": settings.llm_base_url.split("://", 1)[-1].split("/", 1)[0],
        "llm_max_tokens": settings.llm_max_tokens,
        "llm_timeout_seconds": settings.llm_timeout_seconds,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "embedding_base_url_host": settings.embedding_base_url.split("://", 1)[-1].split("/", 1)[0],
        "embedding_dimension": settings.embedding_dimension,
        "embedding_batch_size": settings.embedding_batch_size,
        "qdrant_url": settings.qdrant_url,
        "qdrant_collection_prefix": settings.qdrant_collection_prefix,
        "retriever_mode": settings.retriever_mode,
        "llm_api_key_set": bool(settings.llm_api_key),
        "embedding_api_key_set": bool(settings.embedding_api_key),
    }

    print("[1/5] LLM ...", flush=True)
    RESULT["llm"] = await check_llm(settings, secrets)

    print("[2/5] Embedding ...", flush=True)
    RESULT["embedding"], vector = await check_embedding(settings, secrets)

    print("[3/5] Qdrant isolated collection ...", flush=True)
    RESULT["qdrant"] = await check_qdrant(settings, vector, secrets)

    print("[4/5] Tavily ...", flush=True)
    RESULT["tavily"] = check_tavily()

    print("[5/5] MySQL isolated database ...", flush=True)
    RESULT["mysql"] = check_mysql(secrets)

    RESULT["finishedAt"] = now_iso()
    print(json.dumps(RESULT, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
