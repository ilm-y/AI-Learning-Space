"""Phase 5 Step 3 全量 A/B/C 实验的 Agent 启动器（不修改生产代码）。

在 Pilot 启动器（只读 ``/phase5/llm-diagnostics`` 与 Memory 隔离断言）之上，
额外做三件**只读观测**：

1. 包装 ``OpenAICompatibleAdapter.stream_chat_with_completion``，在每次
   LLM 调用开始时记录实际送入模型的 messages 指纹（规范化 SHA-256），
   在流正常耗尽后读取该次调用自己的 ``StreamDiagnostics``；
2. 统计每次上下文里出现的 ``【Sx-`` 场景标记数量，用于验证「该场景只看到
   自己的数据」；
3. 计数 Embedding 调用次数与文本数（与 pre-Step3 相同的口径）。

通过 ``GET /phase5/prestep3-context-hashes`` 暴露记录（只含哈希、计数、
角色与 usage，不含 prompt 正文、密钥或连接串）。路由只绑定 127.0.0.1。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import Request

_PILOT_DIR = Path(__file__).resolve().parent.parent / "pilot"
if str(_PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PILOT_DIR))
import run_agent as pilot_launcher  # noqa: E402,F401  （诊断路由 + 隔离断言）

from app.embedding.openai_compatible import (  # noqa: E402
    OpenAICompatibleEmbeddingProvider,
)
from app.llm.base import ChatMessage, StreamCompletion  # noqa: E402
from app.llm.openai_compatible import OpenAICompatibleAdapter  # noqa: E402
from app.main import app  # noqa: E402

SCENARIO_MARKER_RE = re.compile(r"【(S\d+)-")
_MAX_RECORDS = 512
_RECORDS: list[dict[str, object]] = []
_EMBEDDING_CALLS = 0
_EMBEDDING_TEXTS = 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_messages(messages: list[ChatMessage]) -> str:
    return json.dumps(
        [message.as_dict() for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _scenario_markers(messages: list[ChatMessage]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        for match in SCENARIO_MARKER_RE.finditer(message.content):
            key = match.group(1)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _message_record(messages: list[ChatMessage], strategy: str) -> dict[str, object]:
    canonical = _canonical_messages(messages)
    system = next((m.content for m in messages if m.role == "system"), "")
    user = next(
        (m.content for m in reversed(messages) if m.role == "user"), ""
    )
    return {
        "seq": len(_RECORDS) + 1,
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "contextStrategy": strategy,
        "contextSha256": _sha256(canonical),
        "messageCount": len(messages),
        "roles": [m.role for m in messages],
        "chars": sum(len(m.content) for m in messages),
        "systemSha256": _sha256(system),
        "userSha256": _sha256(user),
        "scenarioMarkers": _scenario_markers(messages),
        "completed": False,
    }


def _attach_diagnostics(record: dict[str, object], diagnostics) -> None:
    record["completed"] = True
    if diagnostics is None:
        record["usageStatus"] = "UNKNOWN"
        record["usage"] = "UNKNOWN"
        record["diagnostics"] = None
        return
    data = diagnostics.as_dict()
    usage = data.get("usage")
    if usage:
        record["usageStatus"] = "REAL"
        record["usage"] = usage
    else:
        # 与 Pilot 一致：缺失必须写成 UNKNOWN，不能用 0 或本地估算伪造。
        record["usageStatus"] = "UNKNOWN"
        record["usage"] = "UNKNOWN"
    record["diagnostics"] = data


class _RecordingStream:
    """透明代理：只记录，不改变迭代语义与 ``completion`` 语义。"""

    def __init__(self, inner, record: dict[str, object], diagnostics) -> None:
        self._inner = inner
        self._record = record
        self._diagnostics = diagnostics
        self._finished = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException:
            self._finish()
            raise

    async def aclose(self):
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            return await close()
        return None

    @property
    def completion(self):
        return getattr(self._inner, "completion", None)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        _attach_diagnostics(self._record, self._diagnostics)


_original_stream = OpenAICompatibleAdapter.stream_chat_with_completion


def _recorded_stream(self, messages, **kwargs):
    record = _message_record(messages, str(os.environ.get("AILS_CONTEXT_STRATEGY", "")))
    _RECORDS.append(record)
    if len(_RECORDS) > _MAX_RECORDS:
        del _RECORDS[: len(_RECORDS) - _MAX_RECORDS]
    stream = _original_stream(self, messages, **kwargs)
    # 生产方法在返回前同步设置 last_diagnostics，因此这里拿到的是本次调用的
    # 独立诊断对象，而不是「上一次调用」的残留。
    diagnostics = getattr(self, "last_diagnostics", None)
    return _RecordingStream(stream, record, diagnostics)


OpenAICompatibleAdapter.stream_chat_with_completion = _recorded_stream


_original_embed = OpenAICompatibleEmbeddingProvider.embed


async def _counted_embed(self, texts):
    global _EMBEDDING_CALLS, _EMBEDDING_TEXTS
    _EMBEDDING_CALLS += 1
    _EMBEDDING_TEXTS += len(texts)
    return await _original_embed(self, texts)


OpenAICompatibleEmbeddingProvider.embed = _counted_embed


@app.get("/phase5/prestep3-context-hashes")
async def full_context_hashes(request: Request):
    """返回本次进程内所有 LLM 调用的上下文指纹与 usage（不含正文）。"""
    settings = request.app.state.settings
    return {
        "contextStrategy": settings.context_strategy,
        "count": len(_RECORDS),
        "records": list(_RECORDS),
        "embeddingCalls": _EMBEDDING_CALLS,
        "embeddingTexts": _EMBEDDING_TEXTS,
    }


if __name__ == "__main__":
    from app.config import Settings

    settings = Settings()
    if os.environ.get("AILS_CONTEXT_STRATEGY", "C") != settings.context_strategy:
        raise SystemExit("AILS_CONTEXT_STRATEGY 与进程内 Settings 不一致，拒绝启动")
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
