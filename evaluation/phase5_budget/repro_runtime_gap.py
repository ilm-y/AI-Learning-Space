"""Phase 5 边界复现：预算丢弃后，运行时不会拦截「未检索到」误述与越界引用。

这是一个**确定性构造测试**，不是真实模型行为样本：
模型输出由脚本固定，目的是确认生产 `AgentRuntime.generate` 在这些输入下
是否改变回答。它回答的是「系统有没有兜底」，不是「模型通常怎么答」。

运行（在 `agent/` 目录下）：

    python ../evaluation/phase5_budget/repro_runtime_gap.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import AsyncIterator

AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.citations import provided_labels_from_context
from app.config import Settings
from app.context.builder import ContextBuilder
from app.evidence_guard import GUARD_STATUSES
from app.llm.base import ChatMessage
from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.runtime import AgentRuntime
from app.schemas import ChatRequest, Concept, KnowledgeChunk

OUT = Path(__file__).resolve().parent / "repro_runtime_gap.json"

QUESTION = "ORBIT-4721 新部署在失败后等待多久再重试？"
MODEL_OUTPUT = (
    "本轮没有检索到任何相关资料，因此无法确认 ORBIT-4721 的重试窗口。"
    "[资料《orbit_current_p5.md》#0]"
)
CHUNK = KnowledgeChunk(
    id=701,
    document_id=701,
    chunk_index=0,
    document_name="orbit_current_p5.md",
    content=(
        "ORBIT-4721 当前调度规范。当前调度器使用 17 秒的重试窗口，"
        "该值适用于所有新部署。"
    )
    * 20,
)


class FakeLLM:
    name = "scripted"

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield self.answer

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self.answer

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    name = "scripted-retriever"

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return []

    async def get_understandings(self, user_id: int, space_id: int, concept_ids: list[int]):
        return []

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ):
        return []

    async def get_evidences(self, user_id: int, concept_ids: list[int], limit: int):
        return []

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(
            chunks=[CHUNK],
            status=KnowledgeStatus.SUCCESS,
            warning=None,
            candidates=1,
        )

    async def aclose(self) -> None:
        return None


def _request() -> ChatRequest:
    return ChatRequest(
        runId=5100,
        userId=14,
        conversationId=5100,
        learningSpaceId=22,
        userMessage=QUESTION,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    # 80 token 的预算只够本轮问题与状态区块；长知识片段会被整体丢弃。
    builder = ContextBuilder(max_context_tokens=80, recent_history_limit=12)
    settings = Settings(_env_file=None, retriever_mode="fixture", max_context_tokens=80)
    runtime = AgentRuntime(settings, FakeLLM(MODEL_OUTPUT), builder=builder)
    try:
        prepared = await runtime.prepare(_request(), retriever=FakeRetriever())
        deltas: list[str] = []
        async for delta in runtime.generate(prepared):
            deltas.append(delta)
        delivered = "".join(deltas)
        report = runtime.validate_citations(prepared)
        labels = provided_labels_from_context(prepared.context, prepared.state.knowledge)
    finally:
        await runtime.aclose()

    context = prepared.context
    payload = {
        "kind": "constructed_boundary_deterministic",
        "notModelBehavior": True,
        "question": QUESTION,
        "modelOutput": MODEL_OUTPUT,
        "deliveredOutput": delivered,
        "deliveredEqualsModelOutput": delivered == MODEL_OUTPUT,
        "context": {
            "knowledgeStatus": context.knowledge_status,
            "knowledgeState": context.knowledge_state,
            "injectedChunkIds": context.knowledge_injected_chunk_ids,
            "droppedChunkIds": context.knowledge_dropped_chunk_ids,
            "budgetTokens": context.budget_tokens,
            "contextTokens": context.context_tokens,
            "providedLabels": labels,
        },
        "guard": {
            "status": context.knowledge_state,
            "statusInGuardStatuses": context.knowledge_state in GUARD_STATUSES,
            "retrievedChunksNonEmpty": bool(prepared.state.knowledge),
            "noticeAdded": delivered != MODEL_OUTPUT,
            "reason": (
                "generate() 只在 knowledge_state 属于 GUARD_STATUSES 时追加说明；"
                "SUCCESS_PARTIAL / SUCCESS_BUT_DROPPED 的说明只在 Prompt 里，"
                "运行时不会纠正模型对检索状态的误述。"
            ),
        },
        "citations": report.as_dict(),
        "conclusion": {
            "statusMisstatementReachedUser": "没有检索到" in delivered,
            "droppedChunkCitationReachedUser": "[资料《orbit_current_p5.md》#0]" in delivered,
            "citationValidationObservedOnly": report.invalid > 0 and delivered == MODEL_OUTPUT,
        },
        "semanticReview": "NOT_APPLICABLE_DETERMINISTIC",
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
