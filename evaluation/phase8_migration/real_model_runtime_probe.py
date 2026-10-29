"""Phase 8：真实模型 + 真实 AgentRuntime 的端到端截断探针（Python 侧）。

链路：OpenAICompatibleAdapter（真实 DeepSeek）→ Tutor → AgentRuntime.stream
→ internal SSE 事件。只替换检索源为仓库自带的 FixtureRetriever，避免依赖
Spring 后端；模型、适配器、tutor、runtime 都是生产实现。

运行（在 agent/ 目录）：
    PYTHONPATH=. python ../evaluation/phase8_migration/real_model_runtime_probe.py

不打印 API Key。任何一步失败都原样返回非 0，不用伪造 done 事件。
"""

from __future__ import annotations

import asyncio
import os
import sys

# Settings 读环境变量优先于 .env；先固定检索源，避免探针去连 Spring 后端。
os.environ.setdefault("AILS_RETRIEVER_MODE", "fixture")
MAX_TOKENS = int(sys.argv[1]) if len(sys.argv) > 1 else 1024
os.environ["AILS_LLM_MAX_TOKENS"] = str(MAX_TOKENS)

from app.config import Settings  # noqa: E402
from app.llm import build_llm_adapter  # noqa: E402
from app.runtime import AgentRuntime  # noqa: E402
from app.schemas import ChatRequest  # noqa: E402


async def main() -> int:
    settings = Settings()
    print(f"provider={settings.llm_provider}")
    print(f"base_url={settings.llm_base_url}")
    print(f"model={settings.llm_model}")
    print(f"retriever_mode={settings.retriever_mode}")
    print(f"max_tokens={settings.llm_max_tokens}")

    runtime = AgentRuntime(settings, build_llm_adapter(settings))
    request = ChatRequest(
        run_id=9001,
        user_id=1,
        conversation_id=1,
        learning_space_id=1,
        user_message="请详细解释 TLB 的工作原理，至少写 500 字，不要省略。",
    )

    events: list[tuple[str, dict]] = []
    try:
        async for event in runtime.stream(request):
            events.append((event.event, event.data))
    finally:
        await runtime.aclose()

    names = [name for name, _ in events]
    print(f"events={names}")
    done = next((data for name, data in events if name == "done"), None)
    if done is None:
        error = next((data for name, data in events if name == "error"), None)
        print(f"RESULT: FAIL —— 没有 done；error={error}")
        return 1

    print(f"done.finishReason={done.get('finishReason')!r}")
    print(f"done.truncated={done.get('truncated')!r}")
    print(f"done.responseChars={done.get('responseChars')}")

    if done.get("finishReason") == "length" and done.get("truncated") is True:
        print("RESULT: PASS —— runtime 的 done 事件携带 finishReason=length / truncated=true")
        return 0
    print("RESULT: FAIL —— done 事件没有把长度截断传递出来")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
