"""Phase 4 real RAG end-to-end probe.

This is deliberately a *probe*, not a new evaluation framework. It reuses the
existing BackendClient from ``evaluation.rag.runner`` for Spring HTTP calls and
the existing Agent modules for retrieval/context/citation checks.

What it actually runs:

1. Real Spring upload -> async index -> FastAPI parse -> real embedding ->
   MySQL chunk + real Qdrant point -> READY.
2. Real backend ``ready-ids`` allowlist -> real Qdrant filtered search.
3. Real Agent context preview (retrieval + Context Builder, no LLM).
4. A small number of real Spring SSE conversations (real LLM).

It never falls back to hash embeddings or a mock LLM. If the real services are
not configured, it fails instead of producing a PASS.

Synthetic data only. The caller is responsible for deleting the isolated DB
rows / Qdrant collection after the reports are captured.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Make ``evaluation`` and ``agent`` importable when run from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.citations import format_source_label, validate_citations  # noqa: E402
from app.config import Settings  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.internal_identity import mint_identity  # noqa: E402
from app.retriever.base import KnowledgeQuery  # noqa: E402
from app.retriever.knowledge import KnowledgeSearchService  # noqa: E402
from app.vectorstore.qdrant_store import (  # noqa: E402
    QdrantDocumentStore,
    collection_name_for,
)
from evaluation.rag.runner import BackendClient  # noqa: E402

MYSQL = os.environ.get("PHASE4_MYSQL", "mysql")
MYSQL_ARGS = [
    MYSQL,
    "-h",
    os.environ.get("PHASE4_MYSQL_HOST", "127.0.0.1"),
    "-P",
    os.environ.get("PHASE4_MYSQL_PORT", "3306"),
    "-u",
    os.environ.get("PHASE4_MYSQL_USER", "root"),
    os.environ.get("PHASE4_MYSQL_DB", "ails_phase4_e2e"),
    "-N",
    "-B",
    "--raw",
]
BACKEND_URL = os.environ.get("PHASE4_BACKEND_URL", "http://127.0.0.1:8081")
AGENT_URL = os.environ.get("PHASE4_AGENT_URL", "http://127.0.0.1:8000")
INTERNAL_TOKEN = os.environ.get("PHASE4_INTERNAL_TOKEN", "")
IDENTITY_SECRET = os.environ.get("PHASE4_IDENTITY_SECRET", "")


# ===================== synthetic corpus =====================

TARGET_MARKER = "P4-TARGET-7K2"
DISTRACTOR_MARKER = "P4-DISTRACTOR-3M8"
OTHER_SPACE_MARKER = "P4-OTHERSPACE-5Q1"
OTHER_USER_MARKER = "P4-OTHERUSER-8N4"
PROCESSING_MARKER = "P4-PROCESSING-2V6"
FAILED_MARKER = "P4-FAILED-6H9"

TARGET_TEXT = f"""# ORBIT-4721 当前调度规范

本文是 ORBIT-4721 当前部署的现行规范，标记 {TARGET_MARKER}。

当前调度器使用 17 秒的重试窗口（retry window）。该值从 2025 版开始生效，适用于所有新部署。校验种子是 0x7F3A。

运维注意：不要使用历史迁移文档中的旧值。旧值只描述已经下线的调度器。如果监控告警显示 17 秒不符合预期，应先核对部署版本，而不是改回旧值。
"""

DISTRACTOR_TEXT = f"""# ORBIT-4721 历史迁移记录

标记 {DISTRACTOR_MARKER}。2024 年及以前的 ORBIT-4721 旧调度器使用 12 秒的重试窗口（retry window）。该版本已在 2025 年下线。

本记录只用于迁移考古，不适用于当前部署。不要把 12 秒当作现行值。
"""

OTHER_SPACE_TEXT = f"""# ORBIT-4721 归档副本

标记 {OTHER_SPACE_MARKER}。归档副本记录：ORBIT-4721 当前调度器的重试窗口是 17 秒。

这份副本属于另一个学习空间，只能用于归档审计。
"""

OTHER_USER_TEXT = f"""# ORBIT-4721 共享笔记

标记 {OTHER_USER_MARKER}。共享笔记记录：ORBIT-4721 当前调度器的重试窗口是 17 秒。

这份笔记属于另一位用户，不应出现在其他用户的检索结果中。
"""

PROCESSING_TEXT = f"""# Processing canary

{PROCESSING_MARKER}: the processing-only canary value is 41.
"""

FAILED_TEXT = f"""# Failed canary

{FAILED_MARKER}: the failed-only canary value is 83.
"""


# ===================== helpers =====================


def _sql(query: str) -> list[list[str]]:
    password = os.environ.get("PHASE4_MYSQL_PASSWORD")
    if not password:
        raise RuntimeError("缺少 PHASE4_MYSQL_PASSWORD；本脚本不内置数据库密码")
    env = dict(os.environ)
    env["MYSQL_PWD"] = password
    result = subprocess.run(
        MYSQL_ARGS + ["-e", query],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    rows: list[list[str]] = []
    for line in result.stdout.splitlines():
        if line:
            rows.append(line.split("\t"))
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException, limit: int = 500) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:limit]


# ===================== Spring / DB / Qdrant =====================


async def upload_ready(
    backend: BackendClient,
    token: str,
    space_id: int,
    name: str,
    text: str,
) -> dict[str, Any]:
    path = Path(tempfile.gettempdir()) / name
    path.write_text(text, encoding="utf-8")
    document_id = await backend.upload_document(token, space_id, path)
    documents = await backend.wait_ready(token, space_id, [document_id], timeout_seconds=240)
    document = next(item for item in documents if item["id"] == document_id)
    return {
        "document_id": document_id,
        "name": document["name"],
        "file_name": document["fileName"],
        "status": document["status"],
        "chunk_count": document["chunkCount"],
        "indexed_at": document["indexedAt"],
        "embedding_model": document["embeddingModel"],
        "index_version": document["indexVersion"],
    }


def db_document_row(document_id: int) -> dict[str, Any]:
    row = _sql(
        "SELECT id, user_id, learning_space_id, name, file_name, status, chunk_count, "
        "index_attempts, index_error, embedding_model, indexed_at, index_version "
        f"FROM document WHERE id={int(document_id)}"
    )[0]
    return {
        "document_id": int(row[0]),
        "user_id": int(row[1]),
        "space_id": int(row[2]),
        "name": row[3],
        "file_name": row[4],
        "status": row[5],
        "chunk_count": int(row[6]),
        "index_attempts": int(row[7]),
        "index_error": row[8] or None,
        "embedding_model": row[9] or None,
        "indexed_at": row[10] or None,
        "index_version": int(row[11]),
    }


def db_chunks(document_id: int) -> list[dict[str, Any]]:
    rows = _sql(
        "SELECT id, chunk_index, REPLACE(TO_BASE64(content), '\\n', ''), "
        "vector_ref, token_count, "
        "REPLACE(TO_BASE64(COALESCE(metadata_json, '')), '\\n', '') "
        f"FROM document_chunk WHERE document_id={int(document_id)} ORDER BY chunk_index"
    )
    return [
        {
            "chunk_id": int(row[0]),
            "chunk_index": int(row[1]),
            "content": base64.b64decode(row[2]).decode("utf-8"),
            "vector_ref": row[3] or None,
            "token_count": int(row[4]) if row[4] else None,
            "metadata_json": base64.b64decode(row[5]).decode("utf-8") if row[5] else None,
        }
        for row in rows
    ]


async def qdrant_info(client: httpx.AsyncClient, collection: str) -> dict[str, Any]:
    response = await client.get(f"http://127.0.0.1:6333/collections/{collection}")
    response.raise_for_status()
    return response.json()["result"]


async def qdrant_scroll(
    client: httpx.AsyncClient, collection: str, *, limit: int = 1000
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    offset = None
    while True:
        body: dict[str, Any] = {"limit": limit, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        response = await client.post(
            f"http://127.0.0.1:6333/collections/{collection}/points/scroll", json=body
        )
        response.raise_for_status()
        result = response.json()["result"]
        points.extend(result["points"])
        offset = result.get("next_page_offset")
        if offset is None:
            return points


async def qdrant_count(client: httpx.AsyncClient, collection: str, document_id: int) -> int:
    response = await client.post(
        f"http://127.0.0.1:6333/collections/{collection}/points/count",
        json={
            "filter": {
                "must": [
                    {"key": "document_id", "match": {"value": document_id}},
                ]
            },
            "exact": True,
        },
    )
    response.raise_for_status()
    return int(response.json()["result"]["count"])


# ===================== Agent context preview =====================


def preview_body(
    *,
    user_id: int,
    space_id: int,
    token: str,
    question: str,
    history: list[dict[str, str]] | None = None,
    run_id: int = 9001,
) -> dict[str, Any]:
    return {
        "runId": run_id,
        "traceId": "phase4-preview-" + str(run_id),
        "userId": user_id,
        "conversationId": 1,
        "learningSpaceId": space_id,
        "userMessage": question,
        "history": history or [],
        "session": None,
        "authToken": token,
    }


async def agent_preview(
    client: httpx.AsyncClient,
    *,
    user_id: int,
    space_id: int,
    token: str,
    question: str,
    history: list[dict[str, str]] | None = None,
    run_id: int = 9001,
) -> dict[str, Any]:
    identity = mint_identity(
        user_id,
        secret=IDENTITY_SECRET,
        audience="learning-agent",
        ttl_seconds=60,
    )
    response = await client.post(
        f"{AGENT_URL}/agent/context/preview",
        json=preview_body(
            user_id=user_id,
            space_id=space_id,
            token=token,
            question=question,
            history=history,
            run_id=run_id,
        ),
        headers={
            "X-Internal-Token": INTERNAL_TOKEN,
            "X-Internal-Identity": identity,
            "X-Internal-User-Id": str(user_id),
        },
        timeout=60.0,
    )
    body = response.json()
    if response.status_code >= 400 or body.get("code") != 0:
        raise RuntimeError(
            f"context preview HTTP {response.status_code}: {str(body)[:400]}"
        )
    return body["data"]


def injected_knowledge_ids(preview: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for section in preview["context"]["sections"]:
        name = str(section.get("name", ""))
        if name.startswith("knowledge:") and not section.get("dropped"):
            try:
                ids.append(int(name.split(":", 1)[1]))
            except ValueError:
                continue
    return ids


def labels_for(chunks, injected_ids: set[int]) -> list[str]:
    return [format_source_label(chunk) for chunk in chunks if chunk.id in injected_ids]


# ===================== retrieval metrics =====================


def first_relevant_rank(chunks, expected_ids: set[int]) -> int | None:
    for index, chunk in enumerate(chunks, start=1):
        if chunk.id in expected_ids:
            return index
    return None


def citation_summary(answer: str, chunks, injected_ids: set[int]) -> dict[str, Any]:
    labels = labels_for(chunks, injected_ids)
    report = validate_citations(answer, chunks, provided_labels=labels)
    return report.as_dict()


# ===================== main =====================


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if settings.embedding_provider.strip().lower() not in {"openai", "openai-compatible"}:
        raise RuntimeError("Phase 4 必须使用真实 Embedding，拒绝 hash")
    if not settings.embedding_api_key:
        raise RuntimeError("缺少真实 Embedding API Key")
    if settings.llm_provider.strip().lower() != "openai":
        raise RuntimeError("Phase 4 真实模型用例必须使用 openai LLM provider")
    if not settings.llm_api_key:
        raise RuntimeError("缺少真实 LLM API Key")
    if not settings.qdrant_collection_prefix.startswith("ails_phase4_e2e"):
        raise RuntimeError(
            "必须使用 Phase 4 专用 Qdrant prefix（ails_phase4_e2e...），"
            f"当前为 {settings.qdrant_collection_prefix!r}"
        )
    if not INTERNAL_TOKEN or not IDENTITY_SECRET:
        raise RuntimeError("缺少 Phase 4 内部服务凭据")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else (
        REPO_ROOT / "evaluation" / "phase4" / "reports" / stamp
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    state_path = Path(tempfile.gettempdir()) / "phase4-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    user_a = int(state["user_id"])
    space_a1 = int(state["space_id"])
    token_a = state["token"]

    backend = BackendClient(BACKEND_URL)
    embedding = build_embedding_provider(settings)
    store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    collection = collection_name_for(
        settings.qdrant_collection_prefix,
        embedding.model,
        embedding.dimension,
    )
    search = KnowledgeSearchService(
        provider=embedding,
        store=store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )

    http = httpx.AsyncClient(timeout=60.0)
    manifest: dict[str, Any] = {
        "phase": 4,
        "stamp": stamp,
        "backend_url": BACKEND_URL,
        "agent_url": AGENT_URL,
        "qdrant_url": settings.qdrant_url,
        "collection": collection,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": embedding.model,
        "embedding_dimension": embedding.dimension,
        "embedding_batch_size": settings.embedding_batch_size,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "knowledge_top_k": settings.knowledge_top_k,
        "knowledge_min_score": settings.knowledge_min_score,
        "max_context_tokens": settings.max_context_tokens,
        "users": {},
        "spaces": {},
        "documents": {},
    }
    index_checks: list[dict[str, Any]] = []
    retrieval_cases: list[dict[str, Any]] = []
    context_checks: list[dict[str, Any]] = []
    agent_cases: list[dict[str, Any]] = []

    try:
        # ---------- users / spaces ----------
        username_b = f"phase4_b_{stamp}"
        password_b = os.environ.get("PHASE4_B_PASSWORD")
        if not password_b:
            raise RuntimeError("缺少 PHASE4_B_PASSWORD；本脚本不内置测试用户密码")
        token_b, user_b = await backend.register_or_login(
            username_b, password_b
        )
        space_a2 = await backend.create_space(
            token_a, f"Phase4 A2 {stamp}", "synthetic other space"
        )
        space_b1 = await backend.create_space(
            token_b, f"Phase4 B1 {stamp}", "synthetic other user"
        )
        manifest["users"] = {"A": user_a, "B": user_b}
        manifest["spaces"] = {"A1": space_a1, "A2": space_a2, "B1": space_b1}

        # ---------- ready documents ----------
        target = await upload_ready(
            backend, token_a, space_a1, f"orbit_current_{stamp}.md", TARGET_TEXT
        )
        distractor = await upload_ready(
            backend, token_a, space_a1, f"orbit_legacy_{stamp}.md", DISTRACTOR_TEXT
        )
        other_space = await upload_ready(
            backend, token_a, space_a2, f"orbit_archive_{stamp}.md", OTHER_SPACE_TEXT
        )
        other_user = await upload_ready(
            backend, token_b, space_b1, f"orbit_shared_{stamp}.md", OTHER_USER_TEXT
        )
        manifest["documents"] = {
            "target": target,
            "distractor": distractor,
            "other_space": other_space,
            "other_user": other_user,
            "failed_fixture": db_document_row(int(state["failed_document_id"])),
            "processing_fixture": db_document_row(int(state["processing_document_id"])),
        }

        # ---------- index / lifecycle evidence ----------
        info = await qdrant_info(http, collection)
        vectors = info["config"]["params"]["vectors"]
        if isinstance(vectors, dict) and "size" not in vectors:
            # Named vectors: Phase 4 only supports one default vector.
            vectors = next(iter(vectors.values()))
        collection_dim = int(vectors["size"])
        all_points = await qdrant_scroll(http, collection)
        points_by_doc: dict[int, list[dict[str, Any]]] = {}
        for point in all_points:
            payload = point.get("payload") or {}
            doc_id = payload.get("document_id")
            if isinstance(doc_id, int):
                points_by_doc.setdefault(doc_id, []).append(point)

        for role, doc in manifest["documents"].items():
            if role in {"failed_fixture", "processing_fixture"}:
                continue
            document_id = int(doc["document_id"])
            row = db_document_row(document_id)
            chunks = db_chunks(document_id)
            points = points_by_doc.get(document_id, [])
            payloads = [point.get("payload") or {} for point in points]
            payload_by_chunk = {
                int(payload["chunk_id"]): payload
                for payload in payloads
                if isinstance(payload.get("chunk_id"), int)
            }
            checks = {
                "role": role,
                "document_id": document_id,
                "db_status": row["status"],
                "db_chunk_count": row["chunk_count"],
                "db_chunks": len(chunks),
                "db_chunks_with_vector_ref": sum(1 for c in chunks if c["vector_ref"]),
                "qdrant_points": len(points),
                "qdrant_dimension": collection_dim,
                "embedding_dimension": embedding.dimension,
                "indexed_at_present": bool(row["indexed_at"]),
                "embedding_model": row["embedding_model"],
                "payload_matches_db": [],
                "user_space_match": True,
            }
            for chunk in chunks:
                payload = payload_by_chunk.get(chunk["chunk_id"])
                if payload is None:
                    checks["payload_matches_db"].append(
                        {"chunk_id": chunk["chunk_id"], "present": False}
                    )
                    continue
                matches = (
                    payload.get("document_id") == document_id
                    and payload.get("chunk_index") == chunk["chunk_index"]
                    and payload.get("content") == chunk["content"]
                    and payload.get("user_id") == row["user_id"]
                    and payload.get("space_id") == row["space_id"]
                    and payload.get("document_name") == row["name"]
                    and payload.get("file_name") == row["file_name"]
                )
                checks["payload_matches_db"].append(
                    {"chunk_id": chunk["chunk_id"], "present": True, "matches": matches}
                )
                checks["user_space_match"] = checks["user_space_match"] and matches
            checks["payload_count_matches_db"] = len(payloads) == len(chunks)
            checks["vector_refs_nonempty"] = all(c["vector_ref"] for c in chunks)
            checks["chunk_count_matches"] = row["chunk_count"] == len(chunks) == len(points)
            checks["dimension_matches"] = collection_dim == embedding.dimension
            checks["pass"] = all(
                [
                    row["status"] == "READY",
                    row["chunk_count"] == len(chunks) == len(points),
                    checks["payload_count_matches_db"],
                    checks["vector_refs_nonempty"],
                    checks["user_space_match"],
                    checks["chunk_count_matches"],
                    checks["dimension_matches"],
                    all(item.get("matches") for item in checks["payload_matches_db"]),
                ]
            )
            index_checks.append(checks)

        # Non-ready fixtures: no chunks/points, ready-ids excludes them.
        non_ready_checks = []
        for role in ("failed_fixture", "processing_fixture"):
            doc = manifest["documents"][role]
            document_id = int(doc["document_id"])
            row = db_document_row(document_id)
            chunks = db_chunks(document_id)
            points = points_by_doc.get(document_id, [])
            expected_status = "FAILED" if role == "failed_fixture" else "PROCESSING"
            passed = (
                row["status"] == expected_status
                and len(chunks) == 0
                and len(points) == 0
                and row["status"] != "READY"
            )
            non_ready_checks.append(
                {
                    "role": role,
                    "document_id": document_id,
                    "status": row["status"],
                    "expected_status": expected_status,
                    "index_error": row["index_error"],
                    "chunks": len(chunks),
                    "qdrant_points": len(points),
                    "ready": row["status"] == "READY",
                    "pass": passed,
                }
            )
        index_checks.extend(non_ready_checks)

        ready_a1 = await backend.ready_ids(token_a, space_a1, embedding.model)
        ready_a2 = await backend.ready_ids(token_a, space_a2, embedding.model)
        ready_b1 = await backend.ready_ids(token_b, space_b1, embedding.model)
        manifest["ready_ids"] = {
            "A1": ready_a1,
            "A2": ready_a2,
            "B1": ready_b1,
        }
        expected_a1 = {
            int(manifest["documents"]["target"]["document_id"]),
            int(manifest["documents"]["distractor"]["document_id"]),
        }
        ready_filter_checks = {
            "A1_exact_ready_set": set(ready_a1) == expected_a1,
            "A1_excludes_failed": int(manifest["documents"]["failed_fixture"]["document_id"])
            not in ready_a1,
            "A1_excludes_processing": int(
                manifest["documents"]["processing_fixture"]["document_id"]
            )
            not in ready_a1,
            "A1_excludes_other_space": int(
                manifest["documents"]["other_space"]["document_id"]
            )
            not in ready_a1,
            "A1_excludes_other_user": int(
                manifest["documents"]["other_user"]["document_id"]
            )
            not in ready_a1,
            "A2_exact_ready_set": set(ready_a2)
            == {int(manifest["documents"]["other_space"]["document_id"])},
            "B1_exact_ready_set": set(ready_b1)
            == {int(manifest["documents"]["other_user"]["document_id"])},
        }
        index_checks.append({"role": "ready_ids", **ready_filter_checks, "pass": all(ready_filter_checks.values())})

        # ---------- retrieval labels ----------
        def chunk_with_marker(doc_id: int, marker: str) -> int:
            for chunk in db_chunks(doc_id):
                if marker in chunk["content"]:
                    return chunk["chunk_id"]
            raise RuntimeError(f"marker {marker} not found in document {doc_id}")

        labels = {
            "target": chunk_with_marker(int(target["document_id"]), TARGET_MARKER),
            "distractor": chunk_with_marker(int(distractor["document_id"]), DISTRACTOR_MARKER),
            "other_space": chunk_with_marker(
                int(other_space["document_id"]), OTHER_SPACE_MARKER
            ),
            "other_user": chunk_with_marker(
                int(other_user["document_id"]), OTHER_USER_MARKER
            ),
        }
        manifest["labeled_chunks"] = labels

        queries = [
            {
                "key": "supported_paraphrase",
                "space": "A1",
                "token": token_a,
                "user": user_a,
                "space_id": space_a1,
                "allowed": ready_a1,
                "question": "ORBIT-4721 新部署在失败后等待多久再重试？",
                "expected": {labels["target"]},
                "label_basis": "target chunk contains the only current 17-second statement",
            },
            {
                "key": "legacy_version",
                "space": "A1",
                "token": token_a,
                "user": user_a,
                "space_id": space_a1,
                "allowed": ready_a1,
                "question": "2024 年以前 ORBIT-4721 的旧调度器重试窗口是多少秒？",
                "expected": {labels["distractor"]},
                "label_basis": "distractor chunk contains the legacy 12-second statement",
            },
            {
                "key": "supported_with_missing_detail",
                "space": "A1",
                "token": token_a,
                "user": user_a,
                "space_id": space_a1,
                "allowed": ready_a1,
                "question": "ORBIT-4721 当前重试窗口是多少？它的仪表盘默认颜色是什么？",
                "expected": {labels["target"]},
                "label_basis": "target chunk supports the retry-window half only",
            },
            {
                "key": "other_user_scope",
                "space": "B1",
                "token": token_b,
                "user": user_b,
                "space_id": space_b1,
                "allowed": ready_b1,
                "question": "ORBIT-4721 当前调度器的重试窗口是多少秒？",
                "expected": {labels["other_user"]},
                "label_basis": "user B's own READY chunk",
            },
            {
                "key": "other_space_scope",
                "space": "A2",
                "token": token_a,
                "user": user_a,
                "space_id": space_a2,
                "allowed": ready_a2,
                "question": "ORBIT-4721 当前调度器的重试窗口是多少秒？",
                "expected": {labels["other_space"]},
                "label_basis": "user A's other-space READY chunk",
            },
            {
                "key": "no_match",
                "space": "A1",
                "token": token_a,
                "user": user_a,
                "space_id": space_a1,
                "allowed": ready_a1,
                "question": "镓的熔点是多少摄氏度？",
                "expected": set(),
                "label_basis": "no document in A1 is about gallium",
            },
        ]

        for query in queries:
            result = await search.search(
                KnowledgeQuery(
                    user_id=query["user"],
                    space_id=query["space_id"],
                    question=query["question"],
                    top_k=settings.knowledge_top_k,
                    min_score=settings.knowledge_min_score,
                ),
                query["allowed"],
            )
            rank = first_relevant_rank(result.chunks, query["expected"])
            record = {
                "key": query["key"],
                "space": query["space"],
                "question": query["question"],
                "label_basis": query["label_basis"],
                "expected_chunk_ids": sorted(query["expected"]),
                "status": result.status.value,
                "candidates": result.candidates,
                "warning": result.warning,
                "returned": [
                    {
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "chunk_index": chunk.chunk_index,
                        "document_name": chunk.document_name,
                        "score": chunk.score,
                    }
                    for chunk in result.chunks
                ],
                "rank_of_first_expected": rank,
                "recall_at_k": 1.0 if rank is not None else 0.0,
                "reciprocal_rank": (1.0 / rank) if rank else 0.0,
            }
            retrieval_cases.append(record)

        # ---------- Qdrant defense-in-depth filters ----------
        query_vector = (await embedding.embed([queries[0]["question"]]))[0]
        cross_filters: list[dict[str, Any]] = []
        for key, uid, sid, doc_id, expected_zero in [
            ("A_query_with_B_document_id", user_a, space_a1, int(other_user["document_id"]), True),
            ("B_query_with_A_document_id", user_b, space_b1, int(target["document_id"]), True),
            ("A1_query_with_A2_document_id", user_a, space_a1, int(other_space["document_id"]), True),
            ("A1_query_with_A1_document_id", user_a, space_a1, int(target["document_id"]), False),
        ]:
            hits = await store.search_knowledge(
                collection_name=collection,
                query_vector=query_vector,
                user_id=uid,
                space_id=sid,
                document_ids=[doc_id],
                top_k=5,
            )
            cross_filters.append(
                {
                    "key": key,
                    "hits": len(hits),
                    "expected_zero": expected_zero,
                    "pass": (len(hits) == 0) if expected_zero else (len(hits) > 0),
                    "payload_document_ids": [
                        (hit.payload or {}).get("document_id") for hit in hits
                    ],
                }
            )
        retrieval_cases.append({"key": "qdrant_defense_filters", "filters": cross_filters})

        # ---------- context previews ----------
        supported = queries[0]
        supported_result = await search.search(
            KnowledgeQuery(
                user_id=user_a,
                space_id=space_a1,
                question=supported["question"],
                top_k=settings.knowledge_top_k,
                min_score=settings.knowledge_min_score,
            ),
            ready_a1,
        )
        preview = await agent_preview(
            http,
            user_id=user_a,
            space_id=space_a1,
            token=token_a,
            question=supported["question"],
            run_id=9101,
        )
        injected = set(injected_knowledge_ids(preview))
        target_chunk = labels["target"]
        context_checks.append(
            {
                "key": "supported_context",
                "injected_chunk_ids": sorted(injected),
                "expected_target_injected": target_chunk in injected,
                "system_instruction_has_target_label": format_source_label(
                    next(c for c in supported_result.chunks if c.id == target_chunk)
                )
                in preview["context"]["systemInstruction"]
                if any(c.id == target_chunk for c in supported_result.chunks)
                else False,
                "context_tokens": preview["context"]["contextTokens"],
                "budget_tokens": preview["context"]["budgetTokens"],
                "within_budget": preview["context"]["contextTokens"]
                <= preview["context"]["budgetTokens"],
                "dropped_sections": preview["context"]["droppedSections"],
                "knowledge_status_section": any(
                    section["name"] == "knowledge_status"
                    for section in preview["context"]["sections"]
                ),
            }
        )

        # Budget pressure: long history should drop knowledge chunks; check whether
        # the model is still told that retrieval succeeded (the status section only
        # exists for non-SUCCESS in the current implementation).
        long_history = [
            {"role": "USER" if i % 2 == 0 else "ASSISTANT", "content": ("预算压力测试 " + "x" * 900)}
            for i in range(12)
        ]
        budget_preview = await agent_preview(
            http,
            user_id=user_a,
            space_id=space_a1,
            token=token_a,
            question=supported["question"],
            history=long_history,
            run_id=9102,
        )
        budget_injected = set(injected_knowledge_ids(budget_preview))
        context_checks.append(
            {
                "key": "budget_pressure",
                "context_tokens": budget_preview["context"]["contextTokens"],
                "budget_tokens": budget_preview["context"]["budgetTokens"],
                "within_budget": budget_preview["context"]["contextTokens"]
                <= budget_preview["context"]["budgetTokens"],
                "knowledge_injected": sorted(budget_injected),
                "knowledge_all_dropped": not budget_injected,
                "knowledge_status_section_present": any(
                    section["name"] == "knowledge_status"
                    for section in budget_preview["context"]["sections"]
                ),
                "dropped_sections": budget_preview["context"]["droppedSections"],
                "warnings": budget_preview["context"]["warnings"],
            }
        )

        no_match = queries[-1]
        no_match_result = await search.search(
            KnowledgeQuery(
                user_id=user_a,
                space_id=space_a1,
                question=no_match["question"],
                top_k=settings.knowledge_top_k,
                min_score=settings.knowledge_min_score,
            ),
            ready_a1,
        )
        no_match_preview = await agent_preview(
            http,
            user_id=user_a,
            space_id=space_a1,
            token=token_a,
            question=no_match["question"],
            run_id=9103,
        )
        context_checks.append(
            {
                "key": "no_match_context",
                "retrieval_status": no_match_result.status.value,
                "knowledge_injected": sorted(injected_knowledge_ids(no_match_preview)),
                "knowledge_status_section": any(
                    section["name"] == "knowledge_status"
                    for section in no_match_preview["context"]["sections"]
                ),
                "system_instruction": no_match_preview["context"]["systemInstruction"][-500:],
            }
        )

        # ---------- real LLM cases through Spring SSE ----------
        model_cases = [
            {
                "key": "supported",
                "question": "ORBIT-4721 新部署在失败后等待多久再重试？",
                "expected_fact_terms": ["17"],
                "forbidden_as_current_terms": ["12 秒是当前"],
                "required_document_role": "target",
                "hard_requirement": "at least one valid citation to the target document",
            },
            {
                "key": "conflicting_versions",
                "question": "ORBIT-4721 当前重试窗口是多少？请说明旧版本的值是否仍适用。",
                "expected_fact_terms": ["17"],
                "forbidden_as_current_terms": ["当前是 12"],
                "required_document_role": "target",
                "hard_requirement": "at least one valid citation; current value must be 17",
            },
            {
                "key": "insufficient_detail",
                "question": "ORBIT-4721 当前重试窗口是多少？它的仪表盘默认颜色是什么？",
                "expected_fact_terms": ["17"],
                "forbidden_as_current_terms": [],
                "required_document_role": "target",
                "hard_requirement": "citations valid; missing color must not be fabricated",
            },
            {
                "key": "legacy_only",
                "question": "2024 年以前 ORBIT-4721 的旧调度器重试窗口是多少秒？",
                "expected_fact_terms": ["12"],
                "forbidden_as_current_terms": [],
                "required_document_role": "distractor",
                "hard_requirement": "at least one valid citation to the legacy document",
            },
            {
                "key": "no_match",
                "question": "镓的熔点是多少摄氏度？",
                "expected_fact_terms": [],
                "forbidden_as_current_terms": [],
                "required_document_role": None,
                "hard_requirement": "no citations when no document chunk was injected",
            },
        ]

        for index, case in enumerate(model_cases):
            conversation_id = await backend.create_conversation(
                token_a, space_a1, f"Phase4 {case['key']} {stamp}"
            )
            started = time.monotonic()
            events = await backend.stream_message(
                token_a, conversation_id, case["question"], timeout_seconds=300.0
            )
            elapsed = round(time.monotonic() - started, 2)
            deltas = "".join(
                str(payload.get("delta", ""))
                for event, payload in events
                if event == "message.delta"
            )
            errors = [
                payload for event, payload in events if event == "message.error"
            ]
            ended = any(event == "message.end" for event, _ in events)

            preview_case = await agent_preview(
                http,
                user_id=user_a,
                space_id=space_a1,
                token=token_a,
                question=case["question"],
                run_id=9200 + index,
            )
            case_chunks = await search.search(
                KnowledgeQuery(
                    user_id=user_a,
                    space_id=space_a1,
                    question=case["question"],
                    top_k=settings.knowledge_top_k,
                    min_score=settings.knowledge_min_score,
                ),
                ready_a1,
            )
            injected_case = set(injected_knowledge_ids(preview_case))
            citations = citation_summary(deltas, case_chunks.chunks, injected_case)
            cited_ids = {
                int(citation["chunkId"])
                for citation in citations["citations"]
                if citation.get("valid") and citation.get("chunkId") is not None
            }

            hard_failures: list[str] = []
            if not ended:
                hard_failures.append("SSE did not emit message.end")
            if errors:
                hard_failures.append(f"SSE message.error: {errors[0]}")
            if not deltas.strip():
                hard_failures.append("empty answer")
            if citations["malformedCount"]:
                hard_failures.append("malformed citation labels")
            if citations["invalid"]:
                hard_failures.append("invalid citation labels")
            if not cited_ids.issubset(injected_case):
                hard_failures.append("citation to chunk not injected into final context")
            required_role = case["required_document_role"]
            if required_role is not None:
                if not cited_ids:
                    hard_failures.append("no valid citation")
                required_doc = manifest["documents"][required_role]
                required_names = {
                    str(required_doc["file_name"]),
                    str(required_doc["name"]),
                }
                if not required_names.intersection(
                    {str(name) for name in citations["citedDocuments"]}
                ):
                    hard_failures.append(
                        "citation does not point to required document: "
                        + str(required_doc["file_name"])
                    )
            if case["key"] == "no_match" and citations["total"]:
                hard_failures.append("cited a document when no chunk was injected")
            if case["expected_fact_terms"] and not any(
                term in deltas for term in case["expected_fact_terms"]
            ):
                hard_failures.append(
                    "expected fact term missing: " + ",".join(case["expected_fact_terms"])
                )

            agent_cases.append(
                {
                    "key": case["key"],
                    "question": case["question"],
                    "conversation_id": conversation_id,
                    "elapsed_seconds": elapsed,
                    "events": [event for event, _ in events],
                    "answer": deltas,
                    "errors": errors,
                    "ended": ended,
                    "retrieval_status": case_chunks.status.value,
                    "injected_chunk_ids": sorted(injected_case),
                    "knowledge_sections": [
                        {
                            "name": section["name"],
                            "estimated_tokens": section["estimatedTokens"],
                            "dropped": section["dropped"],
                        }
                        for section in preview_case["context"]["sections"]
                        if str(section["name"]).startswith("knowledge")
                    ],
                    "citations": citations,
                    "hard_assertions_passed": not hard_failures,
                    "hard_failures": hard_failures,
                    "semantic_review": "UNREVIEWED",
                    "expected_fact_terms": case["expected_fact_terms"],
                    "fact_terms_present": {
                        term: term in deltas for term in case["expected_fact_terms"]
                    },
                }
            )

        # ---------- summary ----------
        hard_pass = sum(1 for case in agent_cases if case["hard_assertions_passed"])
        metrics_cases = [
            case
            for case in retrieval_cases
            if "expected_chunk_ids" in case
            and case["key"] != "no_match"
            and case["expected_chunk_ids"]
        ]
        recall = (
            round(sum(case["recall_at_k"] for case in metrics_cases) / len(metrics_cases), 4)
            if metrics_cases
            else None
        )
        mrr = (
            round(
                sum(case["reciprocal_rank"] for case in metrics_cases)
                / len(metrics_cases),
                4,
            )
            if metrics_cases
            else None
        )
        budget_check = next(
            (item for item in context_checks if item["key"] == "budget_pressure"), {}
        )
        summary = {
            "index_checks_total": len(index_checks),
            "index_checks_passed": sum(1 for item in index_checks if item.get("pass")),
            "retrieval_labeled_queries": len(metrics_cases),
            "retrieval_k": settings.knowledge_top_k,
            "recall_at_k": recall,
            "mrr": mrr,
            "retrieval_labeling": "one synthetic chunk per labeled query, identified by unique marker",
            "context_checks": len(context_checks),
            "budget_pressure_knowledge_all_dropped": budget_check.get(
                "knowledge_all_dropped"
            ),
            "budget_pressure_status_section_present": budget_check.get(
                "knowledge_status_section_present"
            ),
            "real_model_cases": len(agent_cases),
            "real_model_hard_pass": hard_pass,
            "real_model_hard_fail": len(agent_cases) - hard_pass,
            "semantic_quality": "UNREVIEWED_NO_HUMAN_SCORES",
        }

        _write_json(out_dir / "manifest.json", manifest)
        _write_json(out_dir / "index_results.json", index_checks)
        _write_json(out_dir / "retrieval_results.json", retrieval_cases)
        _write_json(out_dir / "context_results.json", context_checks)
        _write_json(out_dir / "agent_results.json", agent_cases)
        _write_json(out_dir / "summary.json", summary)
        print(json.dumps({"out_dir": str(out_dir), **summary}, ensure_ascii=False, indent=2))
        return 0 if hard_pass == len(agent_cases) and all(
            item.get("pass") for item in index_checks
        ) else 1
    finally:
        await http.aclose()
        await backend.aclose()
        await store.aclose()
        await embedding.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
