"""Pre-Step3 专用 Agent 启动器。

只做两件事，且都不修改生产代码：

1. 复用 ``scripts/phase5/pilot/run_agent.py`` 的只读 LLM 诊断路由
   （``/phase5/llm-diagnostics``），用于记录 provider 返回的 usage /
   finish_reason / requestId；
2. 在进程内包装 ``OpenAICompatibleAdapter.stream_chat_with_completion``，
   对**实际送入模型的 messages** 计算规范化 SHA-256，并通过
   ``GET /phase5/prestep3-context-hashes`` 暴露。

只暴露哈希、条数、角色、字符数与 fixture marker 出现次数；不返回、不落盘
任何 prompt 正文、密钥或连接串。路由只绑定 127.0.0.1，随 Pilot 进程结束。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import Request

# 复用 Pilot 的 Memory 隔离 fail-closed 断言与只读诊断路由。
_PILOT_DIR = Path(__file__).resolve().parent.parent / "pilot"
if str(_PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PILOT_DIR))
import run_agent as pilot_launcher  # noqa: E402,F401  （注册诊断路由 + 隔离断言）

from app.embedding.openai_compatible import (  # noqa: E402
    OpenAICompatibleEmbeddingProvider,
)
from app.llm.base import ChatMessage  # noqa: E402
from app.llm.openai_compatible import OpenAICompatibleAdapter  # noqa: E402
from app.main import app  # noqa: E402

FIXTURE_MARKER = "PHASE5-PRESTEP3-SNAPSHOT"
_MAX_RECORDS = 64
_RECORDS: list[dict[str, object]] = []
_EMBEDDING_CALLS = 0
_EMBEDDING_TEXTS = 0


def _canonical_messages(messages: list[ChatMessage]) -> str:
    return json.dumps(
        [message.as_dict() for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _context_record(messages: list[ChatMessage]) -> dict[str, object]:
    canonical = _canonical_messages(messages)
    system = next(
        (message.content for message in messages if message.role == "system"), ""
    )
    user = next(
        (
            message.content
            for message in reversed(messages)
            if message.role == "user"
        ),
        "",
    )
    marker_count = sum(
        message.content.count(FIXTURE_MARKER) for message in messages
    )
    return {
        "seq": len(_RECORDS) + 1,
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "contextSha256": _sha256(canonical),
        "messageCount": len(messages),
        "roles": [message.role for message in messages],
        "chars": sum(len(message.content) for message in messages),
        "systemSha256": _sha256(system),
        "userSha256": _sha256(user),
        "fixtureMarkerOccurrences": marker_count,
    }


_original_stream = OpenAICompatibleAdapter.stream_chat_with_completion


def _recorded_stream(self, messages, **kwargs):
    record = _context_record(messages)
    _RECORDS.append(record)
    if len(_RECORDS) > _MAX_RECORDS:
        del _RECORDS[: len(_RECORDS) - _MAX_RECORDS]
    # 哈希记录只用于实验观测，不改变调用参数与返回值。
    return _original_stream(self, messages, **kwargs)


OpenAICompatibleAdapter.stream_chat_with_completion = _recorded_stream


_original_embed = OpenAICompatibleEmbeddingProvider.embed


async def _counted_embed(self, texts):
    global _EMBEDDING_CALLS, _EMBEDDING_TEXTS
    _EMBEDDING_CALLS += 1
    _EMBEDDING_TEXTS += len(texts)
    # 只计数，不改变输入、输出与异常语义。
    return await _original_embed(self, texts)


OpenAICompatibleEmbeddingProvider.embed = _counted_embed


@app.get("/phase5/prestep3-context-hashes")
async def prestep3_context_hashes(request: Request):
    """返回最近若干次 LLM 调用的上下文哈希（不含正文）。"""
    settings = request.app.state.settings
    return {
        "contextStrategy": settings.context_strategy,
        "count": len(_RECORDS),
        "records": list(_RECORDS),
        "embeddingCalls": _EMBEDDING_CALLS,
        "embeddingTexts": _EMBEDDING_TEXTS,
    }


if __name__ == "__main__":
    import os

    from app.config import Settings

    settings = Settings()
    if os.environ.get("AILS_CONTEXT_STRATEGY", "C") != settings.context_strategy:
        raise SystemExit("AILS_CONTEXT_STRATEGY 与进程内 Settings 不一致，拒绝启动")
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
