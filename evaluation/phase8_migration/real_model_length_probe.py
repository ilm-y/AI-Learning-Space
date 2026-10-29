"""Phase 8：用真实模型验证 finish_reason=length 是否被适配器正确识别。

不是单元测试，也不修改任何仓库代码：直接读 agent/.env 里的真实 LLM 配置，
给一个必然超过 16 token 的提问，观察 OpenAICompatibleAdapter 返回的
StreamCompletion.finish_reason 与 truncated。

运行（在仓库根目录）：
    PYTHONPATH=agent python evaluation/phase8_migration/real_model_length_probe.py

不打印 API Key。失败时原样打印错误，不用 mock 结果代替。
"""

from __future__ import annotations

import asyncio
import sys

from app.config import Settings
from app.llm import ChatMessage, build_llm_adapter

MAX_TOKENS = int(sys.argv[1]) if len(sys.argv) > 1 else 16


async def main() -> int:
    settings = Settings()
    # 只打印非敏感字段，避免把密钥写进日志
    print(f"provider={settings.llm_provider}")
    print(f"base_url={settings.llm_base_url}")
    print(f"model={settings.llm_model}")
    print(f"request_max_tokens={MAX_TOKENS}")

    adapter = build_llm_adapter(settings)
    messages = [
        ChatMessage("user", "请详细解释 TLB 的工作原理，至少写 500 字，不要省略。")
    ]

    stream = adapter.stream_chat_with_completion(messages, max_tokens=MAX_TOKENS)
    deltas: list[str] = []
    async for delta in stream:
        deltas.append(delta)

    completion = stream.completion
    if completion is None:
        print("RESULT: completion=None（流未正常耗尽，不能判定为正常完成）")
        return 1

    print(f"deltas={len(deltas)} chars={sum(len(d) for d in deltas)}")
    print(f"metadata_available={completion.metadata_available}")
    print(f"finish_reason={completion.finish_reason!r}")
    print(f"truncated={completion.truncated}")

    if completion.finish_reason == "length" and completion.truncated:
        print("RESULT: PASS —— 真实模型在 Token 上限处返回 length，适配器标记为截断")
        return 0
    print("RESULT: FAIL —— 真实模型没有给出 length，或适配器未识别")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
