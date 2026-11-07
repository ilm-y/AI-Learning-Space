"""Phase 4：Memory 检索召回率离线评测。

本脚本**不修改任何 Java 生产代码**，只在独立的评测库 / Qdrant collection 中
复现三种检索策略：

* ``KeywordRetriever``  —— MySQL FULLTEXT ``MATCH ... AGAINST``（与生产
  ``MemoryMapper.searchFulltext`` 使用同一张表、同一个 ngram 索引）。
* ``DenseRetriever``    —— 复用 Agent 生产代码的 Embedding + Qdrant 记忆检索
  （``MemoryVectorSearch`` / ``QdrantMemoryStore``）。
* ``HybridRetriever``   —— 两路并行召回后做 RRF（k=60），再回 MySQL 复核
  ``user_id / status='ACTIVE'``，与 ``HybridSearchService`` 的融合规则一致。

评测指标：Recall@5、MRR@5、P95 延迟，并单独统计无关查询的“空结果率”。

隔离设计
--------
* MySQL：专用库 ``ails_memory_eval``（默认重置），不碰开发库。
* Qdrant：专用 collection 前缀 ``ails_memory_eval``，不碰 ``ails_memories_*``。
* 所有 memory id / user id 都是评测专用固定值。

用法
----
::

    MYSQL_PASSWORD=... python -m evaluation.memory.run_eval

或::

    python evaluation/memory/run_eval.py --mysql-password ...

凭据只从命令行 / 环境变量读取，不会写入报告或日志。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
for _path in (str(REPO_ROOT), str(AGENT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import httpx  # noqa: E402
import pymysql  # noqa: E402
from qdrant_client import AsyncQdrantClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.embedding.factory import build_embedding_provider  # noqa: E402
from app.memories.indexer import MemoryIndexer, content_hash_for  # noqa: E402
from app.memories.schemas import MemorySearchRequest, MemorySyncRequest  # noqa: E402
from app.memories.search import MemoryVectorSearch  # noqa: E402
from app.vectorstore.memory_store import QdrantMemoryStore  # noqa: E402

LOG = logging.getLogger("memory-eval")

DEFAULT_DATABASE = "ails_memory_eval"
DEFAULT_COLLECTION_PREFIX = "ails_memory_eval"
DEFAULT_QUERIES = REPO_ROOT / "evaluation" / "memory" / "datasets" / "memory_queries.jsonl"
DEFAULT_FIXTURES = REPO_ROOT / "evaluation" / "memory" / "datasets" / "memory_fixtures.jsonl"
DEFAULT_SCHEMA = REPO_ROOT / "backend" / "backend" / "src" / "main" / "resources" / "schema.sql"
DEFAULT_OUTPUT = REPO_ROOT / "evaluation" / "memory" / "reports" / "latest.json"
DEFAULT_MARKDOWN = REPO_ROOT / "evaluation" / "memory" / "reports" / "latest.md"

PRIMARY_USER_ID = 9001
OTHER_USER_ID = 9002
SPACE_ID = 8001
CONCEPT_ID = 8101
USER_IDS = {"primary": PRIMARY_USER_ID, "other": OTHER_USER_ID}


# ======================================================================
# 数据模型
# ======================================================================


@dataclass(frozen=True)
class MemoryFixture:
    memory_id: int
    user: str
    status: str
    scope_type: str
    memory_type: str
    importance: float
    category: str
    query_id: str | None
    content: str


@dataclass(frozen=True)
class QueryCase:
    query_id: str
    category: str
    query: str
    relevant_memory_ids: tuple[int, ...]
    note: str


@dataclass
class RetrievalResult:
    ids: list[int]
    scores: list[float]
    elapsed_ms: float
    status: str = "OK"
    warnings: list[str] = field(default_factory=list)
    raw_ids: list[int] = field(default_factory=list)
    raw_scores: list[float] = field(default_factory=list)


# ======================================================================
# 通用工具
# ======================================================================


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON: {exc}") from exc
    return rows


def load_fixtures(path: Path) -> list[MemoryFixture]:
    fixtures = []
    for row in load_jsonl(path):
        fixtures.append(
            MemoryFixture(
                memory_id=int(row["memory_id"]),
                user=str(row["user"]),
                status=str(row["status"]),
                scope_type=str(row["scope_type"]),
                memory_type=str(row["memory_type"]),
                importance=float(row["importance"]),
                category=str(row.get("category", "")),
                query_id=row.get("query_id"),
                content=str(row["content"]),
            )
        )
    return fixtures


def load_queries(path: Path) -> list[QueryCase]:
    cases = []
    for row in load_jsonl(path):
        cases.append(
            QueryCase(
                query_id=str(row["query_id"]),
                category=str(row["category"]),
                query=str(row["query"]),
                relevant_memory_ids=tuple(int(x) for x in row["relevant_memory_ids"]),
                note=str(row.get("note", "")),
            )
        )
    return cases


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank 百分位；样本为空时返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(p / 100.0 * len(ordered)) - 1)
    return float(ordered[index])


def mean(values: Sequence[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


def safe_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise SystemExit(f"非法标识符：{name!r}")
    return name


# ======================================================================
# MySQL：建库、建表、灌数据
# ======================================================================


def connect_mysql(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str | None = None,
) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def split_sql_statements(script: str) -> list[str]:
    """按分号拆分 DDL，正确处理引号、``--`` 与 ``/* */`` 注释。

    schema.sql 没有存储过程 / 触发器 / DELIMITER，因此不需要完整的 SQL 解析器。
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(script)
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False

    while index < length:
        char = script[index]
        next_char = script[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                buffer.append(char)
            index += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue

        if quote is not None:
            buffer.append(char)
            if char == "\\" and quote in ("'", '"') and index + 1 < length:
                buffer.append(script[index + 1])
                index += 2
                continue
            if char == quote:
                if index + 1 < length and script[index + 1] == quote:
                    buffer.append(script[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in ("'", '"', "`"):
            quote = char
            buffer.append(char)
            index += 1
            continue

        if char == "-" and next_char == "-":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        if char == "#":
            in_line_comment = True
            index += 1
            continue

        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def reset_eval_database(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> None:
    safe_identifier(database)
    if not database.startswith(DEFAULT_DATABASE):
        raise SystemExit(
            f"拒绝重置数据库 {database!r}：库名必须以 {DEFAULT_DATABASE!r} 开头"
        )
    server = connect_mysql(host, port, user, password)
    try:
        with server.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.execute(
                f"CREATE DATABASE `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        server.close()


def apply_schema(connection: pymysql.connections.Connection, schema_path: Path) -> None:
    script = schema_path.read_text(encoding="utf-8")
    statements = split_sql_statements(script)
    with connection.cursor() as cursor:
        for statement in statements:
            if not statement.strip():
                continue
            cursor.execute(statement)
    LOG.info("schema 应用完成：%s 条语句", len(statements))


def seed_mysql(
    connection: pymysql.connections.Connection,
    fixtures: Sequence[MemoryFixture],
    reset: bool,
) -> None:
    with connection.cursor() as cursor:
        if not reset:
            cursor.execute(
                "DELETE FROM memory WHERE user_id IN (%s, %s)",
                (PRIMARY_USER_ID, OTHER_USER_ID),
            )
            cursor.execute("DELETE FROM concept WHERE id = %s", (CONCEPT_ID,))
            cursor.execute("DELETE FROM learning_space WHERE id = %s", (SPACE_ID,))
            cursor.execute(
                "DELETE FROM `user` WHERE id IN (%s, %s)",
                (PRIMARY_USER_ID, OTHER_USER_ID),
            )

        cursor.execute(
            "INSERT INTO `user` (id, username, password_hash) VALUES (%s, %s, %s)",
            (PRIMARY_USER_ID, "memory-eval-primary", "x"),
        )
        cursor.execute(
            "INSERT INTO `user` (id, username, password_hash) VALUES (%s, %s, %s)",
            (OTHER_USER_ID, "memory-eval-other", "x"),
        )
        cursor.execute(
            "INSERT INTO learning_space (id, user_id, name, visibility) "
            "VALUES (%s, %s, %s, 'PRIVATE')",
            (SPACE_ID, PRIMARY_USER_ID, "memory-eval-space"),
        )
        cursor.execute(
            "INSERT INTO concept (id, learning_space_id, canonical_name) "
            "VALUES (%s, %s, %s)",
            (CONCEPT_ID, SPACE_ID, "memory-eval-concept"),
        )

        rows = []
        for fixture in fixtures:
            user_id = USER_IDS[fixture.user]
            rows.append(
                (
                    fixture.memory_id,
                    user_id,
                    fixture.scope_type,
                    "GLOBAL",
                    None,
                    None,
                    fixture.memory_type,
                    f"eval-{fixture.memory_id}",
                    fixture.content,
                    fixture.status,
                    fixture.importance,
                    0,
                )
            )
        cursor.executemany(
            """
            INSERT INTO memory
                (id, user_id, scope_type, scope_key, learning_space_id, concept_id,
                 memory_type, memory_key, content, status, importance, version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    LOG.info("MySQL 种子数据写入完成：%s 条 memory", len(fixtures))


def mysql_environment(connection: pymysql.connections.Connection) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT VERSION() AS version")
        version = str(cursor.fetchone()["version"])
        cursor.execute("SHOW VARIABLES LIKE 'ngram_token_size'")
        row = cursor.fetchone()
        ngram = int(row["Value"]) if row else None
    return {"version": version, "ngram_token_size": ngram}


# ======================================================================
# Qdrant：重置 / 灌数据
# ======================================================================


async def reset_eval_collection(
    *,
    url: str,
    api_key: str,
    timeout_seconds: float,
    collection_name: str,
) -> None:
    if not collection_name.startswith(DEFAULT_COLLECTION_PREFIX):
        raise SystemExit(
            f"拒绝删除 collection {collection_name!r}：名称必须以 "
            f"{DEFAULT_COLLECTION_PREFIX!r} 开头"
        )
    client = AsyncQdrantClient(
        url=url,
        api_key=api_key or None,
        timeout=max(1, int(timeout_seconds)),
        prefer_grpc=False,
        check_compatibility=False,
    )
    try:
        if await client.collection_exists(collection_name):
            await client.delete_collection(collection_name)
            LOG.info("已删除旧评测 collection：%s", collection_name)
    finally:
        close = getattr(client, "aclose", None) or getattr(client, "close")
        await close()


async def qdrant_version(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url.rstrip('/')}/")
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("version", ""))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("读取 Qdrant 版本失败：%s", type(exc).__name__)
        return ""


async def seed_qdrant(
    *,
    store: QdrantMemoryStore,
    provider: Any,
    fixtures: Sequence[MemoryFixture],
) -> None:
    indexer = MemoryIndexer(provider=provider, store=store)
    for position, fixture in enumerate(fixtures, 1):
        request = MemorySyncRequest(
            operation="UPSERT",
            memoryId=fixture.memory_id,
            userId=USER_IDS[fixture.user],
            content=fixture.content,
            contentHash=content_hash_for(fixture.content),
            memoryVersion=0,
            scopeType=fixture.scope_type,
            memoryType=fixture.memory_type,
            importance=fixture.importance,
            status=fixture.status,
        )
        await indexer.sync(request)
        if position % 10 == 0 or position == len(fixtures):
            LOG.info("Qdrant 种子进度：%s/%s", position, len(fixtures))
    LOG.info("Qdrant 种子数据写入完成：%s 条 memory", len(fixtures))


# ======================================================================
# 三种 Retriever
# ======================================================================


class KeywordRetriever:
    """MySQL FULLTEXT 检索；SQL 与生产 ``searchFulltext`` 的过滤/排序一致。"""

    name = "keyword"

    def __init__(
        self,
        connection: pymysql.connections.Connection,
        *,
        user_id: int,
        recall_k: int,
    ) -> None:
        self._connection = connection
        self._user_id = user_id
        self._recall_k = recall_k

    def search(self, query: str) -> RetrievalResult:
        started = time.perf_counter()
        sql = """
            SELECT id,
                   MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE) AS relevance
            FROM memory
            WHERE user_id = %s
              AND status = 'ACTIVE'
              AND MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE)
            ORDER BY relevance DESC, importance DESC, updated_at DESC, id ASC
            LIMIT %s
        """
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, (query, self._user_id, query, self._recall_k))
                rows = cursor.fetchall()
            ids = [int(row["id"]) for row in rows]
            scores = [float(row["relevance"]) for row in rows]
            return RetrievalResult(ids=ids, scores=scores, elapsed_ms=elapsed_ms(started), raw_ids=ids, raw_scores=scores)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("keyword 检索失败 query=%r", query)
            return RetrievalResult(
                ids=[],
                scores=[],
                elapsed_ms=elapsed_ms(started),
                status=f"ERROR:{type(exc).__name__}",
                warnings=[str(exc)],
            )


class DenseRetriever:
    """Embedding + Qdrant；复用生产 ``MemoryVectorSearch``。

    生产代码的 ``memory_search_min_score`` 默认 0.0（RRF 只用排名）。本评测为了
    让“无关查询不该召回任何结果”可度量，额外在**客户端**施加统一阈值 τ；
    原始（未过滤）结果也会保留，供报告做阈值敏感性分析。
    """

    name = "dense"

    def __init__(
        self,
        vector_search: MemoryVectorSearch,
        *,
        user_id: int,
        recall_k: int,
        min_score: float,
    ) -> None:
        self._vector_search = vector_search
        self._user_id = user_id
        self._recall_k = recall_k
        self._min_score = min_score

    async def search(self, query: str) -> RetrievalResult:
        started = time.perf_counter()
        try:
            payload = await self._vector_search.search(
                MemorySearchRequest(
                    user_id=self._user_id,
                    query=query,
                    recall_k=self._recall_k,
                )
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("dense 检索失败 query=%r", query)
            return RetrievalResult(
                ids=[],
                scores=[],
                elapsed_ms=elapsed_ms(started),
                status=f"ERROR:{type(exc).__name__}",
                warnings=[str(exc)],
            )

        hits = payload.get("hits", [])
        raw_ids = [int(hit["memoryId"]) for hit in hits]
        raw_scores = [float(hit["score"]) for hit in hits]
        kept = [
            (memory_id, score)
            for memory_id, score in zip(raw_ids, raw_scores)
            if score >= self._min_score
        ]
        return RetrievalResult(
            ids=[item[0] for item in kept],
            scores=[item[1] for item in kept],
            elapsed_ms=elapsed_ms(started),
            warnings=list(payload.get("warnings", [])),
            raw_ids=raw_ids,
            raw_scores=raw_scores,
        )


class HybridRetriever:
    """并行调用 Keyword + Dense，RRF(k=60) 融合，再回 MySQL 复核。"""

    name = "hybrid"

    def __init__(
        self,
        *,
        keyword: KeywordRetriever,
        dense: DenseRetriever,
        connection: pymysql.connections.Connection,
        user_id: int,
        recall_k: int,
        rrf_k: int,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
    ) -> None:
        self._keyword = keyword
        self._dense = dense
        self._connection = connection
        self._user_id = user_id
        self._recall_k = recall_k
        self._rrf_k = rrf_k
        self._lexical_weight = lexical_weight
        self._vector_weight = vector_weight

    async def search(self, query: str) -> RetrievalResult:
        started = time.perf_counter()
        keyword_result, dense_result = await asyncio.gather(
            asyncio.to_thread(self._keyword.search, query),
            self._dense.search(query),
            return_exceptions=True,
        )
        if isinstance(keyword_result, BaseException):
            keyword_result = RetrievalResult(
                ids=[], scores=[], elapsed_ms=0.0,
                status=f"ERROR:{type(keyword_result).__name__}",
                warnings=[str(keyword_result)],
            )
        if isinstance(dense_result, BaseException):
            dense_result = RetrievalResult(
                ids=[], scores=[], elapsed_ms=0.0,
                status=f"ERROR:{type(dense_result).__name__}",
                warnings=[str(dense_result)],
            )

        warnings = [*keyword_result.warnings, *dense_result.warnings]
        channel_errors = [
            result.status
            for result in (keyword_result, dense_result)
            if result.status != "OK"
        ]
        if channel_errors:
            status = "DEGRADED" if len(channel_errors) == 1 else "UNAVAILABLE"
        else:
            status = "OK"

        fused = rrf_fuse(
            keyword_result.ids,
            dense_result.ids,
            k=self._rrf_k,
            lexical_weight=self._lexical_weight,
            vector_weight=self._vector_weight,
        )
        if not fused:
            return RetrievalResult(
                ids=[], scores=[], elapsed_ms=elapsed_ms(started),
                status=status, warnings=warnings,
            )

        ids = [memory_id for memory_id, _ in fused]
        visible = self._recheck(ids)
        ordered = [
            (memory_id, score)
            for memory_id, score in fused
            if memory_id in visible
        ]
        ordered.sort(
            key=lambda item: (
                -item[1],
                -(visible[item[0]]["importance"] or 0.0),
                -(
                    visible[item[0]]["updated_at"].timestamp()
                    if visible[item[0]]["updated_at"]
                    else 0.0
                ),
                item[0],
            )
        )
        return RetrievalResult(
            ids=[item[0] for item in ordered],
            scores=[item[1] for item in ordered],
            elapsed_ms=elapsed_ms(started),
            status=status,
            warnings=warnings,
        )

    def _recheck(self, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """与 ``MemoryMapper.selectVisibleByIds`` 的 ACTIVE / user 过滤一致。

        本评测固定 ``space_id = None``，因此没有 SPACE / CONCEPT 可见性过滤；
        跨用户与 ARCHIVED 记忆仍然在这里被剔除。
        """
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        sql = (
            "SELECT id, importance, updated_at FROM memory "
            f"WHERE id IN ({placeholders}) "
            "AND user_id = %s AND status = 'ACTIVE'"
        )
        with self._connection.cursor() as cursor:
            cursor.execute(sql, (*ids, self._user_id))
            rows = cursor.fetchall()
        return {int(row["id"]): row for row in rows}


def rrf_fuse(
    lexical_ids: Sequence[int],
    vector_ids: Sequence[int],
    *,
    k: int,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
) -> list[tuple[int, float]]:
    """RRF：``Σ w / (k + rank)``，rank 从 1 开始；同路重复 id 只计第一次。"""
    scores: dict[int, float] = {}
    for ids, weight in ((lexical_ids, lexical_weight), (vector_ids, vector_weight)):
        seen: set[int] = set()
        rank = 0
        for memory_id in ids:
            if memory_id in seen:
                continue
            seen.add(memory_id)
            rank += 1
            scores[memory_id] = scores.get(memory_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


# ======================================================================
# 指标
# ======================================================================


def score_query(
    retrieved_ids: Sequence[int],
    relevant_ids: Sequence[int],
    *,
    k: int = 5,
) -> dict[str, Any]:
    top = list(retrieved_ids[:k])
    if not relevant_ids:
        return {
            "applicable": False,
            "recall_at_k": None,
            "rr": None,
            "empty": len(top) == 0,
            "returned": len(top),
            "first_relevant_rank": None,
        }
    relevant = set(relevant_ids)
    first_rank = next(
        (rank for rank, memory_id in enumerate(top, 1) if memory_id in relevant),
        None,
    )
    return {
        "applicable": True,
        "recall_at_k": 1.0 if first_rank is not None else 0.0,
        "rr": (1.0 / first_rank) if first_rank is not None else 0.0,
        "empty": len(top) == 0,
        "returned": len(top),
        "first_relevant_rank": first_rank,
    }


def aggregate_metrics(
    per_query: Sequence[dict[str, Any]],
    retriever_name: str,
    *,
    k: int,
) -> dict[str, Any]:
    answerable = [row for row in per_query if row["category"] != "irrelevant"]
    irrelevant = [row for row in per_query if row["category"] == "irrelevant"]

    def rows(category: str) -> list[dict[str, Any]]:
        return [row for row in per_query if row["category"] == category]

    def category_block(category: str) -> dict[str, Any]:
        selected = rows(category)
        scores = [row["retrievers"][retriever_name]["metrics"] for row in selected]
        latencies = [
            sample
            for row in selected
            for sample in row["retrievers"][retriever_name]["latency_samples_ms"]
        ]
        if category == "irrelevant":
            empty_rate = mean([1.0 if item["empty"] else 0.0 for item in scores])
            return {
                "count": len(selected),
                "empty_rate": empty_rate,
                "false_positive_rate": 1.0 - empty_rate,
                "mean_returned": mean([item["returned"] for item in scores]),
                "p95_latency_ms": percentile(latencies, 95),
                "mean_latency_ms": mean(latencies),
            }
        return {
            "count": len(selected),
            "recall_at_5": mean([item["recall_at_k"] for item in scores]),
            "mrr_at_5": mean([item["rr"] for item in scores]),
            "p95_latency_ms": percentile(latencies, 95),
            "mean_latency_ms": mean(latencies),
        }

    answerable_scores = [
        row["retrievers"][retriever_name]["metrics"] for row in answerable
    ]
    irrelevant_scores = [
        row["retrievers"][retriever_name]["metrics"] for row in irrelevant
    ]
    latencies = [
        sample
        for row in per_query
        for sample in row["retrievers"][retriever_name]["latency_samples_ms"]
    ]
    statuses: dict[str, int] = {}
    for row in per_query:
        status = row["retrievers"][retriever_name]["status"]
        statuses[status] = statuses.get(status, 0) + 1

    return {
        "recall_at_5": mean([item["recall_at_k"] for item in answerable_scores]),
        "mrr_at_5": mean([item["rr"] for item in answerable_scores]),
        "answerable_count": len(answerable),
        "irrelevant_empty_rate": mean(
            [1.0 if item["empty"] else 0.0 for item in irrelevant_scores]
        ),
        "irrelevant_false_positive_rate": 1.0
        - mean([1.0 if item["empty"] else 0.0 for item in irrelevant_scores]),
        "irrelevant_mean_returned": mean(
            [item["returned"] for item in irrelevant_scores]
        ),
        "p95_latency_ms": percentile(latencies, 95),
        "mean_latency_ms": mean(latencies),
        "median_latency_ms": statistics.median(latencies) if latencies else 0.0,
        "latency_samples": len(latencies),
        "statuses": statuses,
        "by_category": {
            "literal": category_block("literal"),
            "synonym": category_block("synonym"),
            "irrelevant": category_block("irrelevant"),
        },
    }


def threshold_sensitivity(
    per_query: Sequence[dict[str, Any]],
    thresholds: Sequence[float],
    *,
    dense_retriever_name: str = "dense",
    k: int = 5,
) -> list[dict[str, Any]]:
    """用 Dense 的原始分数做后验阈值敏感性分析（不是主结果）。"""
    rows = []
    for threshold in thresholds:
        answerable_recall = []
        answerable_mrr = []
        irrelevant_empty = []
        for row in per_query:
            dense = row["retrievers"][dense_retriever_name]
            kept = [
                memory_id
                for memory_id, score in zip(dense["raw_ids"], dense["raw_scores"])
                if score >= threshold
            ]
            metrics = score_query(kept, row["relevant_memory_ids"], k=k)
            if row["category"] == "irrelevant":
                irrelevant_empty.append(1.0 if metrics["empty"] else 0.0)
            else:
                answerable_recall.append(metrics["recall_at_k"])
                answerable_mrr.append(metrics["rr"])
        rows.append(
            {
                "threshold": threshold,
                "recall_at_5": mean(answerable_recall),
                "mrr_at_5": mean(answerable_mrr),
                "irrelevant_empty_rate": mean(irrelevant_empty),
            }
        )
    return rows


# ======================================================================
# 主流程
# ======================================================================


async def run(args: argparse.Namespace) -> dict[str, Any]:
    queries = load_queries(Path(args.queries))
    fixtures = load_fixtures(Path(args.fixtures))
    if len(queries) != 30:
        raise SystemExit(f"评测集应为 30 条，实际 {len(queries)} 条")
    categories = {row.category for row in queries}
    if categories != {"literal", "synonym", "irrelevant"}:
        raise SystemExit(f"未知 category：{categories}")

    settings = Settings()
    if settings.embedding_provider == "hash" and not args.allow_hash_embedding:
        raise SystemExit(
            "当前 Embedding provider 是 hash（离线占位向量）。"
            "本评测需要真实语义向量；如确认只做离线冒烟，请加 --allow-hash-embedding。"
        )
    qdrant_url = args.qdrant_url or settings.qdrant_url
    store = QdrantMemoryStore(
        url=qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=args.collection_prefix,
    )
    provider = build_embedding_provider(settings)
    collection_name = store.collection_name(
        model=provider.model, dimension=provider.dimension
    )
    vector_search = MemoryVectorSearch(
        provider=provider,
        store=store,
        min_score=0.0,  # 阈值在评测脚本里显式施加，保留原始分数
        timeout_seconds=max(30.0, settings.memory_search_timeout_seconds),
        default_top_k=args.recall_k,
    )

    reset = not args.no_reset
    mysql_server = connect_mysql(
        args.mysql_host, args.mysql_port, args.mysql_user, args.mysql_password
    )
    try:
        if reset:
            reset_eval_database(
                args.mysql_host,
                args.mysql_port,
                args.mysql_user,
                args.mysql_password,
                args.database,
            )
        else:
            with mysql_server.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{safe_identifier(args.database)}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
    finally:
        mysql_server.close()

    connection = connect_mysql(
        args.mysql_host,
        args.mysql_port,
        args.mysql_user,
        args.mysql_password,
        args.database,
    )
    try:
        apply_schema(connection, Path(args.schema))
        seed_mysql(connection, fixtures, reset=reset)
        mysql_env = mysql_environment(connection)

        if reset:
            await reset_eval_collection(
                url=qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout_seconds=settings.qdrant_timeout_seconds,
                collection_name=collection_name,
            )
        await store.ensure_collection(
            model=provider.model, dimension=provider.dimension
        )
        await seed_qdrant(store=store, provider=provider, fixtures=fixtures)

        keyword = KeywordRetriever(
            connection, user_id=PRIMARY_USER_ID, recall_k=args.recall_k
        )
        dense = DenseRetriever(
            vector_search,
            user_id=PRIMARY_USER_ID,
            recall_k=args.recall_k,
            min_score=args.min_score,
        )
        hybrid = HybridRetriever(
            keyword=keyword,
            dense=dense,
            connection=connection,
            user_id=PRIMARY_USER_ID,
            recall_k=args.recall_k,
            rrf_k=args.rrf_k,
        )

        # 预热：避免首次 HTTP/TLS 建连把 P95 拉高；预热结果丢弃。
        LOG.info("预热三种检索路径……")
        await dense.search("预热查询")
        await hybrid.search("预热查询")
        keyword.search("预热查询")

        per_query: list[dict[str, Any]] = []
        for index, case in enumerate(queries, 1):
            LOG.info("[%s/%s] %s %s", index, len(queries), case.query_id, case.query)
            retrievers: dict[str, Any] = {}
            for retriever in (keyword, dense, hybrid):
                samples: list[float] = []
                first: RetrievalResult | None = None
                stable = True
                for repetition in range(args.repeats):
                    if retriever is keyword:
                        result = retriever.search(case.query)
                    else:
                        result = await retriever.search(case.query)
                    samples.append(result.elapsed_ms)
                    if first is None:
                        first = result
                    elif result.ids != first.ids:
                        stable = False
                assert first is not None
                metrics = score_query(first.ids, case.relevant_memory_ids, k=args.top_k)
                retrievers[retriever.name] = {
                    "ids": first.ids,
                    "scores": first.scores,
                    "raw_ids": first.raw_ids,
                    "raw_scores": first.raw_scores,
                    "status": first.status,
                    "warnings": first.warnings,
                    "latency_samples_ms": samples,
                    "metrics": metrics,
                    "stable_across_repeats": stable,
                }

            per_query.append(
                {
                    "query_id": case.query_id,
                    "category": case.category,
                    "query": case.query,
                    "relevant_memory_ids": list(case.relevant_memory_ids),
                    "note": case.note,
                    "retrievers": retrievers,
                }
            )

        metrics = {
            name: aggregate_metrics(per_query, name, k=args.top_k)
            for name in ("keyword", "dense", "hybrid")
        }
        sensitivity = threshold_sensitivity(
            per_query,
            thresholds=(0.30, 0.35, 0.40, 0.42, 0.45, 0.50, 0.55),
            k=args.top_k,
        )
        unstable = [
            f"{row['query_id']}:{name}"
            for row in per_query
            for name, value in row["retrievers"].items()
            if not value["stable_across_repeats"]
        ]

        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run": {
                "queries": len(queries),
                "fixtures": len(fixtures),
                "primary_user_id": PRIMARY_USER_ID,
                "other_user_id": OTHER_USER_ID,
                "database": args.database,
                "collection": collection_name,
                "top_k": args.top_k,
                "recall_k": args.recall_k,
                "rrf_k": args.rrf_k,
                "min_score": args.min_score,
                "repeats": args.repeats,
                "reset": reset,
            },
            "environment": {
                "python": sys.version.split()[0],
                "mysql": mysql_env,
                "qdrant_version": await qdrant_version(qdrant_url),
                "embedding_provider": provider.name,
                "embedding_model": provider.model,
                "embedding_dimension": provider.dimension,
                "embedding_fingerprint": provider.fingerprint,
                "qdrant_url": qdrant_url,
            },
            "dataset": {
                "literal": sum(1 for case in queries if case.category == "literal"),
                "synonym": sum(1 for case in queries if case.category == "synonym"),
                "irrelevant": sum(1 for case in queries if case.category == "irrelevant"),
                "primary_active_memories": sum(
                    1
                    for fixture in fixtures
                    if fixture.user == "primary" and fixture.status == "ACTIVE"
                ),
                "distractors": sum(
                    1 for fixture in fixtures if fixture.category == "distractor"
                ),
                "cross_user_negatives": sum(
                    1 for fixture in fixtures if fixture.category == "cross_user_negative"
                ),
                "archived_negatives": sum(
                    1 for fixture in fixtures if fixture.category == "archived_negative"
                ),
            },
            "metrics": metrics,
            "threshold_sensitivity": sensitivity,
            "per_query": per_query,
            "quality_checks": {
                "unstable_across_repeats": unstable,
                "all_retrieval_paths_ok": all(
                    status == "OK"
                    for row in per_query
                    for value in row["retrievers"].values()
                    for status in [value["status"]]
                ),
            },
        }
        return result
    finally:
        connection.close()
        await store.aclose()
        await provider.aclose()


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Memory 检索离线评测结果（自动生成）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- Embedding：`{result['environment']['embedding_model']}` "
        f"({result['environment']['embedding_dimension']}d)",
        f"- MySQL：`{result['environment']['mysql']['version']}` "
        f"(ngram_token_size={result['environment']['mysql']['ngram_token_size']})",
        f"- Qdrant：`{result['environment']['qdrant_version']}`",
        f"- 数据集：30 条（10 字面 / 10 同义 / 10 无关）；"
        f"主用户 ACTIVE 记忆 {result['dataset']['primary_active_memories']} 条，"
        f"其中干扰项 {result['dataset']['distractors']} 条",
        f"- 融合：RRF k={result['run']['rrf_k']}；Dense 相关度阈值 τ={result['run']['min_score']}",
        "",
        "## 主结果",
        "",
        "| Retriever | Recall@5 (20 可回答) | MRR@5 (20 可回答) | 无关查询空结果率 (10) | P95 延迟 (ms) | 平均延迟 (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("keyword", "dense", "hybrid"):
        item = result["metrics"][name]
        lines.append(
            f"| {name} | {item['recall_at_5']:.3f} | {item['mrr_at_5']:.3f} | "
            f"{item['irrelevant_empty_rate']:.3f} | {item['p95_latency_ms']:.1f} | "
            f"{item['mean_latency_ms']:.1f} |"
        )
    lines += [
        "",
        "## 分类别",
        "",
        "| Retriever | 字面 Recall@5 | 字面 MRR@5 | 同义 Recall@5 | 同义 MRR@5 | 无关空结果率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("keyword", "dense", "hybrid"):
        item = result["metrics"][name]
        literal = item["by_category"]["literal"]
        synonym = item["by_category"]["synonym"]
        irrelevant = item["by_category"]["irrelevant"]
        lines.append(
            f"| {name} | {literal['recall_at_5']:.3f} | {literal['mrr_at_5']:.3f} | "
            f"{synonym['recall_at_5']:.3f} | {synonym['mrr_at_5']:.3f} | "
            f"{irrelevant['empty_rate']:.3f} |"
        )
    lines += [
        "",
        "## 阈值敏感性（Dense 原始分数的后验分析）",
        "",
        "| τ | Recall@5 | MRR@5 | 无关空结果率 |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for row in result["threshold_sensitivity"]:
        lines.append(
            f"| {row['threshold']:.2f} | {row['recall_at_5']:.3f} | "
            f"{row['mrr_at_5']:.3f} | {row['irrelevant_empty_rate']:.3f} |"
        )
    lines += [
        "",
        "## 逐查询 Top-5（节选）",
        "",
        "| Query | 类别 | Keyword | Dense | Hybrid | 目标 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["per_query"]:
        if row["query_id"] not in {
            "L1", "L3", "S1", "S2", "S3", "S4", "S6", "S8", "S9", "I2"
        }:
            continue
        cells = []
        for name in ("keyword", "dense", "hybrid"):
            ids = row["retrievers"][name]["ids"][:5]
            cells.append(", ".join(str(memory_id) for memory_id in ids) or "—")
        lines.append(
            f"| {row['query_id']} `{row['query']}` | {row['category']} | "
            f"{cells[0]} | {cells[1]} | {cells[2]} | "
            f"{row['relevant_memory_ids'] or '—'} |"
        )
    lines += [
        "",
        "## 质量检查",
        "",
        f"- 所有检索路径状态为 OK：{result['quality_checks']['all_retrieval_paths_ok']}",
        f"- 重复运行结果不一致：{result['quality_checks']['unstable_across_repeats'] or '无'}",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Memory 检索召回率离线评测（Keyword / Dense / Hybrid）"
    )
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN))
    parser.add_argument("--mysql-host", default=os.environ.get("AILS_EVAL_MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--mysql-port", type=int, default=int(os.environ.get("AILS_EVAL_MYSQL_PORT", "3306")))
    parser.add_argument("--mysql-user", default=os.environ.get("AILS_EVAL_MYSQL_USER", "root"))
    parser.add_argument(
        "--mysql-password",
        default=(
            os.environ.get("AILS_EVAL_MYSQL_PASSWORD")
            or os.environ.get("MYSQL_PASSWORD")
            or ""
        ),
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--qdrant-url", default=os.environ.get("AILS_EVAL_QDRANT_URL"))
    parser.add_argument("--collection-prefix", default=DEFAULT_COLLECTION_PREFIX)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--recall-k", type=int, default=50)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--min-score", type=float, default=0.42)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--allow-hash-embedding", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.mysql_password:
        LOG.warning(
            "未提供 MySQL 密码（--mysql-password / MYSQL_PASSWORD）。"
            "如果本机 root 无密码可继续，否则会连接失败。"
        )
    if args.repeats < 1:
        raise SystemExit("--repeats 必须 >= 1")
    if args.top_k < 1 or args.recall_k < args.top_k:
        raise SystemExit("要求 recall_k >= top_k >= 1")

    result = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(result)
    markdown_path = Path(args.markdown_output)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")

    print(markdown)
    print(f"\nJSON 结果：{output}")
    print(f"Markdown 摘要：{markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
