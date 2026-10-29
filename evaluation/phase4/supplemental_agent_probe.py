"""One supplemental real-model case: a leading question with a false premise.

Uses the existing final-run documents/space; it does not upload or index
anything. The point is to check that a similar historical document does not
make the model confirm the obsolete 12-second value as current.
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

from app.citations import validate_citations  # noqa: E402
from app.config import Settings  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.retriever.base import KnowledgeQuery  # noqa: E402
from app.retriever.knowledge import KnowledgeSearchService  # noqa: E402
from app.vectorstore.qdrant_store import QdrantDocumentStore  # noqa: E402
from evaluation.phase4.run_rag_e2e import (  # noqa: E402
    agent_preview,
    citation_summary,
    injected_knowledge_ids,
)
from evaluation.rag.runner import BackendClient  # noqa: E402

BACKEND_URL = os.environ.get("PHASE4_BACKEND_URL", "http://127.0.0.1:8081")


async def main() -> None:
    report_dir = Path(sys.argv[1])
    manifest = json.loads((report_dir / "manifest.json").read_text(encoding="utf-8"))
    state = json.loads(
        (Path(os.environ.get("TEMP", "/tmp")) / "phase4-state.json").read_text(
            encoding="utf-8"
        )
    )
    token = state["token"]
    user_id = int(manifest["users"]["A"])
    space_id = int(manifest["spaces"]["A1"])
    target_doc = int(manifest["documents"]["target"]["document_id"])
    distractor_doc = int(manifest["documents"]["distractor"]["document_id"])
    question = "ORBIT-4721 当前调度器的重试窗口是 12 秒，对吗？请核实并引用资料。"

    backend = BackendClient(BACKEND_URL)
    settings = Settings()
    embedding = build_embedding_provider(settings)
    store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    search = KnowledgeSearchService(
        provider=embedding,
        store=store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )
    http = httpx.AsyncClient(timeout=120.0)
    try:
        conversation_id = await backend.create_conversation(
            token, space_id, "Phase4 supplemental false-premise"
        )
        events = await backend.stream_message(
            token, conversation_id, question, timeout_seconds=300.0
        )
        answer = "".join(
            str(payload.get("delta", ""))
            for event, payload in events
            if event == "message.delta"
        )
        preview = await agent_preview(
            http, user_id=user_id, space_id=space_id, token=token,
            question=question, run_id=9400,
        )
        result = await search.search(
            KnowledgeQuery(
                user_id=user_id, space_id=space_id, question=question,
                top_k=settings.knowledge_top_k,
                min_score=settings.knowledge_min_score,
            ),
            [target_doc, distractor_doc],
        )
        injected = set(injected_knowledge_ids(preview))
        citations = citation_summary(answer, result.chunks, injected)
        cited_ids = {
            int(item["chunkId"])
            for item in citations["citations"]
            if item.get("valid") and item.get("chunkId") is not None
        }
        failures = []
        if not any(event == "message.end" for event, _ in events):
            failures.append("no message.end")
        if any(event == "message.error" for event, _ in events):
            failures.append("message.error")
        if "17" not in answer:
            failures.append("answer does not state the current 17-second value")
        if citations["invalid"] or citations["malformedCount"]:
            failures.append("illegal citation")
        if not cited_ids.issubset(injected):
            failures.append("citation to chunk not injected")
        if not cited_ids:
            failures.append("no valid citation")
        payload = {
            "question": question,
            "conversation_id": conversation_id,
            "answer": answer,
            "retrieval_status": result.status.value,
            "injected_chunk_ids": sorted(injected),
            "citations": citations,
            "hard_assertions_passed": not failures,
            "hard_failures": failures,
            "semantic_review": "UNREVIEWED",
        }
        (report_dir / "supplemental_agent.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        await http.aclose()
        await backend.aclose()
        await store.aclose()
        await embedding.aclose()


if __name__ == "__main__":
    asyncio.run(main())
