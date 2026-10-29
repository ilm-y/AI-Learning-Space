"""Phase 4.1 F1：预算压力下的真实模型回答（受控上下文，非在线检索）。

Phase 4 只记录了「检索 SUCCESS、知识全被预算丢弃、没有状态说明」的 Context
preview，没有在该上下文下调用真实模型。本脚本用同一组合成 chunk 与长历史，
在修复后的 Context Builder 上调用真实 LLM，产出盲评所需回答。

边界：
- 检索结果是固定的合成片段，**没有**再次调用 Qdrant/Embedding；
- 模型调用是真实的（provider=openai，来自 AILS_* 配置），不是 Mock；
- 本脚本不打印、不落盘任何密钥。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.context.builder import ContextBuilder
from app.citations import provided_labels_from_context, validate_citations
from app.llm import ChatMessage, build_llm_adapter
from app.schemas import HistoryMessage, KnowledgeChunk, MemoryItem, MemoryType, MessageRole
from app.state.agent_state import AgentState
from app.trace import RunTrace

OUT = Path(__file__).resolve().parent / "budget_pressure_llm.json"

QUESTION = "ORBIT-4721 新部署在失败后等待多久再重试？"

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


def _memories() -> list[MemoryItem]:
    """4 条合成长期记忆，用于在默认 3000 token 预算下稳定挤掉知识区块。

    它们与 Phase 4 的「长历史」同一用途：制造真实存在的上下文压力，不参与
    事实判断。记忆正文不含 ORBIT-4721 的任何数值。
    """
    text = ("这是用于占据上下文预算的合成长期记忆，不包含 ORBIT-4721 的重试窗口"
            "数值或现行规范结论。") * 12
    return [
        MemoryItem(
            id=900 + index,
            memory_type=MemoryType.PREFERENCE,
            content=f"[合成记忆 {index + 1}] {text}"[:500],
        )
        for index in range(4)
    ]


def _long_history() -> list[HistoryMessage]:
    """与 Phase 4 预算压力 preview 同量级的长历史：12 条、每条约 900 字符。"""
    filler = (
        "这是本轮隔离测试中用于占据上下文预算的合成历史，不包含 ORBIT-4721 "
        "的重试窗口数值，也不包含任何现行规范结论。"
    )
    body = filler * 24
    messages: list[HistoryMessage] = []
    for index in range(12):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        messages.append(
            HistoryMessage(
                role=role,
                content=f"[合成历史 {index + 1}] {body}",
            )
        )
    return messages


async def main() -> None:
    settings = Settings()
    if settings.llm_provider != "openai" or not settings.llm_api_key:
        raise SystemExit("真实 LLM 未配置：需要 AILS_LLM_PROVIDER=openai 且提供 API Key")

    builder = ContextBuilder(
        max_context_tokens=settings.max_context_tokens,
        recent_history_limit=settings.recent_history_limit,
    )
    state = AgentState(
        run_id=9001,
        user_id=3,
        conversation_id=9001,
        space_id=3,
        user_message=QUESTION,
        history=_long_history(),
        memories=_memories(),
        knowledge=[CURRENT, LEGACY],
        knowledge_status="SUCCESS",
        trace=RunTrace(run_id=9001),
    )
    context = builder.build(state)

    labels = provided_labels_from_context(context, state.knowledge)
    adapter = build_llm_adapter(settings)
    try:
        answer = await adapter.complete(
            [ChatMessage(role=message.role, content=message.content) for message in context.messages],
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    finally:
        await adapter.aclose()

    report = validate_citations(answer, state.knowledge, provided_labels=labels)
    payload = {
        "question": QUESTION,
        "questionKind": "retrieval_success_but_context_budget_insufficient",
        "model": settings.llm_model,
        "retrievalSource": "fixed_synthetic_chunks_from_phase4 (no online retrieval)",
        "pressureSource": "12 synthetic history messages + 4 synthetic memories",
        "knowledgeChunks": [
            {
                "chunkId": chunk.id,
                "documentId": chunk.document_id,
                "documentName": chunk.document_name,
                "content": chunk.content,
            }
            for chunk in state.knowledge
        ],
        "context": {
            "knowledgeStatus": context.knowledge_status,
            "knowledgeState": context.knowledge_state,
            "injectedChunkIds": context.knowledge_injected_chunk_ids,
            "droppedChunkIds": context.knowledge_dropped_chunk_ids,
            "contextTokens": context.context_tokens,
            "budgetTokens": context.budget_tokens,
            "estimatedTokens": context.estimated_tokens,
            "providedLabels": labels,
            "warnings": context.warnings,
        },
        "systemInstruction": context.messages[0].content,
        "answer": answer,
        "citations": report.as_dict(),
        "semanticReview": "PENDING",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(OUT),
                "knowledgeState": context.knowledge_state,
                "contextTokens": context.context_tokens,
                "answerChars": len(answer),
                "citations": report.total,
                "valid": report.valid,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
