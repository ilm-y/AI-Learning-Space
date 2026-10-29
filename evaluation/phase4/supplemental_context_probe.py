"""Supplemental Context Builder checks against the final Phase 4 run.

Reads the final run manifest, then makes real `/agent/context/preview` calls for
user A's other space and user B's space. This verifies that permission filtering
is visible in the *final context*, not only in a raw Qdrant search.

It does not call the LLM and does not create documents.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
for path in (REPO_ROOT, AGENT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.citations import format_source_label  # noqa: E402
from app.internal_identity import mint_identity  # noqa: E402
from app.retriever.base import KnowledgeQuery  # noqa: E402
from app.retriever.knowledge import KnowledgeSearchService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.vectorstore.qdrant_store import QdrantDocumentStore, collection_name_for  # noqa: E402
from evaluation.rag.runner import BackendClient  # noqa: E402
from evaluation.phase4.run_rag_e2e import agent_preview, injected_knowledge_ids  # noqa: E402

BACKEND_URL = os.environ.get("PHASE4_BACKEND_URL", "http://127.0.0.1:8081")
IDENTITY_SECRET = os.environ.get("PHASE4_IDENTITY_SECRET", "")
INTERNAL_TOKEN = os.environ.get("PHASE4_INTERNAL_TOKEN", "")
B_PASSWORD = os.environ.get("PHASE4_B_PASSWORD", "")


async def main() -> None:
    report_dir = Path(sys.argv[1])
    manifest = json.loads((report_dir / "manifest.json").read_text(encoding="utf-8"))
    state = json.loads(
        (Path(os.environ.get("TEMP", "/tmp")) / "phase4-state.json").read_text(
            encoding="utf-8"
        )
    )
    token_a = state["token"]
    user_a = int(manifest["users"]["A"])
    user_b = int(manifest["users"]["B"])
    space_a1 = int(manifest["spaces"]["A1"])
    space_a2 = int(manifest["spaces"]["A2"])
    space_b1 = int(manifest["spaces"]["B1"])
    labeled = manifest["labeled_chunks"]
    target_chunk = int(labeled["target"])
    distractor_chunk = int(labeled["distractor"])
    other_space_chunk = int(labeled["other_space"])
    other_user_chunk = int(labeled["other_user"])
    target_doc = int(manifest["documents"]["target"]["document_id"])
    distractor_doc = int(manifest["documents"]["distractor"]["document_id"])

    backend = BackendClient(BACKEND_URL)
    username_b = None
    # The synthetic B user name is timestamped; find it by the same prefix.
    # The DB is isolated, so a prefix query is safe and does not touch real data.
    import subprocess

    password = os.environ.get("PHASE4_MYSQL_PASSWORD")
    if not password:
        raise RuntimeError("缺少 PHASE4_MYSQL_PASSWORD；本脚本不内置数据库密码")
    if not B_PASSWORD:
        raise RuntimeError("缺少 PHASE4_B_PASSWORD；本脚本不内置测试用户密码")
    env = dict(os.environ)
    env["MYSQL_PWD"] = password
    rows = subprocess.run(
        [
            "mysql", "-h", "127.0.0.1", "-P", "3306", "-u", "root",
            os.environ.get("PHASE4_MYSQL_DB", "ails_phase4_e2e"),
            "-N", "-B", "--raw", "-e",
            f"SELECT username FROM user WHERE id={user_b}",
        ],
        capture_output=True, text=True, env=env, check=True,
    ).stdout.strip()
    username_b = rows.splitlines()[0]
    token_b, logged_b = await backend.register_or_login(username_b, B_PASSWORD)
    if logged_b != user_b:
        raise RuntimeError(f"B login returned user {logged_b}, expected {user_b}")

    settings = Settings()
    embedding = build_embedding_provider(settings)
    store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    collection = collection_name_for(
        settings.qdrant_collection_prefix, embedding.model, embedding.dimension
    )
    search = KnowledgeSearchService(
        provider=embedding,
        store=store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )
    http = httpx.AsyncClient(timeout=60.0)
    checks: list[dict] = []

    try:
        cases = [
            ("A1", user_a, space_a1, token_a, "ORBIT-4721 当前调度器的重试窗口是多少秒？",
             {target_chunk, distractor_chunk}),
            ("A2", user_a, space_a2, token_a, "ORBIT-4721 当前调度器的重试窗口是多少秒？",
             {other_space_chunk}),
            ("B1", user_b, space_b1, token_b, "ORBIT-4721 当前调度器的重试窗口是多少秒？",
             {other_user_chunk}),
        ]
        for index, (label, uid, sid, token, question, expected) in enumerate(cases):
            preview = await agent_preview(
                http, user_id=uid, space_id=sid, token=token, question=question,
                run_id=9300 + index,
            )
            injected = injected_knowledge_ids(preview)
            labels = [
                line for line in preview["context"]["systemInstruction"].splitlines()
                if "资料《" in line
            ]
            checks.append(
                {
                    "space": label,
                    "injected_chunk_ids": sorted(injected),
                    "expected_chunk_ids": sorted(expected),
                    "matches_expected": set(injected) == expected,
                    "knowledge_section_names": [
                        section["name"]
                        for section in preview["context"]["sections"]
                        if section["name"].startswith("knowledge:")
                    ],
                    "system_instruction_knowledge_lines": labels,
                }
            )

        # History order / priorities: two history messages, then the question.
        history = [
            {"role": "USER", "content": "历史问题：先记下 ORBIT-4721。"},
            {"role": "ASSISTANT", "content": "历史回答：已记下。"},
        ]
        preview = await agent_preview(
            http, user_id=user_a, space_id=space_a1, token=token_a,
            question="ORBIT-4721 当前重试窗口是多少？", history=history, run_id=9311,
        )
        sections = preview["context"]["sections"]
        checks.append(
            {
                "space": "history_and_priorities",
                "section_priorities": {
                    section["name"]: section["priority"] for section in sections
                },
                "message_roles_and_contents": [
                    {"role": message["role"], "content": message["content"][:80]}
                    for message in preview["context"]["messages"]
                ],
                "last_message_is_question": preview["context"]["messages"][-1][
                    "content"
                ].startswith("ORBIT-4721 当前重试窗口"),
                "history_order_preserved": [
                    message["content"] for message in preview["context"]["messages"][-3:-1]
                ]
                == [item["content"] for item in history],
            }
        )

        # Label-to-chunk mapping for A1: every injected label must resolve to the
        # exact DB chunk ID; no label may point at a chunk outside the context.
        question = cases[0][4]
        chunks = (
            await search.search(
                KnowledgeQuery(
                    user_id=user_a, space_id=space_a1, question=question,
                    top_k=settings.knowledge_top_k,
                    min_score=settings.knowledge_min_score,
                ),
                [target_doc, distractor_doc],
            )
        ).chunks
        preview = await agent_preview(
            http, user_id=user_a, space_id=space_a1, token=token_a,
            question=question, run_id=9312,
        )
        injected = set(injected_knowledge_ids(preview))
        expected_labels = {
            format_source_label(chunk): chunk.id for chunk in chunks if chunk.id in injected
        }
        labels_in_prompt = [
            label
            for label in expected_labels
            if label in preview["context"]["systemInstruction"]
        ]
        checks.append(
            {
                "space": "label_mapping",
                "expected_labels": expected_labels,
                "labels_present_in_prompt": labels_in_prompt,
                "all_labels_present": len(labels_in_prompt) == len(expected_labels),
                "label_chunk_ids": sorted(expected_labels.values()),
                "injected_chunk_ids": sorted(injected),
            }
        )
    finally:
        await http.aclose()
        await backend.aclose()
        await store.aclose()
        await embedding.aclose()

    (report_dir / "supplemental_context.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
