"""Phase 4.1 F3：修复后的真实模型来源归属样本（受控上下文）。

与 Phase 4 不同，本脚本使用**修改后的 Prompt**（要求不同来源/版本的事实拆句
并分别标注），在无预算压力的受控上下文里重新调用真实模型，观察两个
`numericConflict=true` 形态是否仍出现。

边界：
- 知识片段固定复用 Phase 4 的合成 chunk，不重新调用 Qdrant/Embedding；
- 模型调用是真实的（来自 AILS_* 配置），不是 Mock；
- 不打印、不落盘任何密钥；
- 只记录可机器检查的引用结构，不给出语义正确性结论。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.citations import provided_labels_from_context, validate_citations
from app.config import Settings
from app.context.builder import ContextBuilder
from app.llm import ChatMessage, build_llm_adapter
from app.schemas import KnowledgeChunk
from app.state.agent_state import AgentState
from app.trace import RunTrace

OUT = Path(__file__).resolve().parent / "source_attribution_llm.json"

CURRENT = KnowledgeChunk(
    id=18,
    document_id=20,
    chunk_index=0,
    document_name="orbit_current_20261027_172403.md",
    content=(
        "ORBIT-4721 当前调度规范\n\n"
        "本文是 ORBIT-4721 当前部署的现行规范，标记 P4-TARGET-7K2。\n\n"
        "当前调度器使用 17 秒的重试窗口（retry window）。该值从 2025 版开始生效，"
        "适用于所有新部署。校验种子是 0x7F3A。\n\n"
        "运维注意：不要使用历史迁移文档中的旧值。旧值只描述已经下线的调度器。"
        "如果监控告警显示 17 秒不符合预期，应先核对部署版本，而不是改回旧值。"
    ),
)
LEGACY = KnowledgeChunk(
    id=19,
    document_id=21,
    chunk_index=0,
    document_name="orbit_legacy_20261027_172403.md",
    content=(
        "ORBIT-4721 历史迁移记录\n\n"
        "标记 P4-DISTRACTOR-3M8。2024 年及以前的 ORBIT-4721 旧调度器使用 12 秒的"
        "重试窗口（retry window）。该版本已在 2025 年下线。\n\n"
        "本记录只用于迁移考古，不适用于当前部署。不要把 12 秒当作现行值。"
    ),
)

CASES = [
    {
        "key": "insufficient_detail",
        "question": "ORBIT-4721 当前重试窗口是多少？它的仪表盘默认颜色是什么？",
    },
    {
        "key": "legacy_only",
        "question": "2024 年以前 ORBIT-4721 的旧调度器重试窗口是多少秒？",
    },
]

TRIALS = 3


async def main() -> None:
    settings = Settings()
    if settings.llm_provider != "openai" or not settings.llm_api_key:
        raise SystemExit("真实 LLM 未配置：需要 AILS_LLM_PROVIDER=openai 且提供 API Key")

    builder = ContextBuilder(
        max_context_tokens=settings.max_context_tokens,
        recent_history_limit=settings.recent_history_limit,
    )
    adapter = build_llm_adapter(settings)
    results = []
    run_id = 9100
    try:
        for case in CASES:
            for trial in range(1, TRIALS + 1):
                run_id += 1
                state = AgentState(
                    run_id=run_id,
                    user_id=3,
                    conversation_id=run_id,
                    space_id=3,
                    user_message=case["question"],
                    history=[],
                    memories=[],
                    knowledge=[CURRENT, LEGACY],
                    knowledge_status="SUCCESS",
                    trace=RunTrace(run_id=run_id),
                )
                context = builder.build(state)
                labels = provided_labels_from_context(context, state.knowledge)
                answer = await adapter.complete(
                    [ChatMessage(role=message.role, content=message.content) for message in context.messages],
                    temperature=settings.llm_temperature,
                    max_tokens=settings.llm_max_tokens,
                )
                report = validate_citations(answer, state.knowledge, provided_labels=labels)
                results.append(
                    {
                        "key": case["key"],
                        "trial": trial,
                        "question": case["question"],
                        "knowledgeState": context.knowledge_state,
                        "injectedChunkIds": context.knowledge_injected_chunk_ids,
                        "droppedChunkIds": context.knowledge_dropped_chunk_ids,
                        "contextTokens": context.context_tokens,
                        "budgetTokens": context.budget_tokens,
                        "providedLabels": labels,
                        "answer": answer,
                        "citations": report.as_dict(),
                    }
                )
                print(
                    json.dumps(
                        {
                            "case": case["key"],
                            "trial": trial,
                            "state": context.knowledge_state,
                            "answerChars": len(answer),
                            "citations": report.total,
                            "valid": report.valid,
                            "numericConflict": report.numeric_conflict_count,
                            "unsupported": report.unsupported_count,
                        },
                        ensure_ascii=False,
                    )
                )
    finally:
        await adapter.aclose()

    summary = []
    for case in CASES:
        rows = [row for row in results if row["key"] == case["key"]]
        summary.append(
            {
                "key": case["key"],
                "trials": len(rows),
                "numericConflictTrials": sum(
                    1 for row in rows if row["citations"]["numericConflictCount"] > 0
                ),
                "unsupportedTrials": sum(
                    1 for row in rows if row["citations"]["unsupportedCount"] > 0
                ),
                "invalidCitations": sum(
                    row["citations"]["invalid"] for row in rows
                ),
            }
        )

    payload = {
        "model": settings.llm_model,
        "promptVersion": "phase4.1-source-attribution",
        "trialsPerCase": TRIALS,
        "summary": summary,
        "retrievalSource": "fixed_synthetic_chunks_from_phase4 (no online retrieval)",
        "knowledgeChunks": [
            {
                "chunkId": chunk.id,
                "documentId": chunk.document_id,
                "documentName": chunk.document_name,
                "content": chunk.content,
            }
            for chunk in (CURRENT, LEGACY)
        ],
        "cases": results,
        "semanticReview": "PENDING",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
