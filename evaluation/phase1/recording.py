"""记录型 LLM 适配器。

Phase 1 的一个核心问题是「这次回答到底有没有真的调用模型」。产品当前没有
把 provider / model / token usage 写进 SSE 或 trace，所以评测不能靠猜。
这一层包在真实 Adapter 外面，记录**调用事实**，但绝不记录完整 context、
Authorization 或敏感用户内容：

- 只存 prompt 的字符数与 SHA-256，不存 prompt 原文；
- 原始输出截断到 4000 字符并做密钥/邮箱脱敏；
- token usage 适配器没有暴露时明确写 ``null``，不编造数字。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.context.tokens import estimate_tokens
from app.llm.base import ChatMessage, LLMAdapter, LLMError

MAX_RAW_OUTPUT = 4000

_REDACTIONS = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9._\-]+"), "sk-***"),
    (
        re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
        r"\1=***",
    ),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),
)


def redact(text: str) -> str:
    value = text or ""
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


@dataclass
class LLMCallRecord:
    case_id: str
    request_id: str
    call_index: int
    adapter_name: str
    provider: str
    model: str
    classification: str
    method: str
    call_emitted: bool = False
    output_emitted: bool = False
    http_status: int | None = None
    latency_ms: int = 0
    error_type: str | None = None
    error_message: str | None = None
    prompt_chars: int = 0
    prompt_sha256: str = ""
    estimated_prompt_tokens: int = 0
    message_roles: list[str] = field(default_factory=list)
    system_chars: int = 0
    output_chars: int = 0
    raw_output: str | None = None
    raw_output_truncated: bool = False
    token_usage: dict | None = None
    token_usage_note: str = "adapter 未暴露 usage；只有字符数与估算 token"
    stream_diagnostics: dict | None = None
    completion_diagnostics: dict | None = None

    def as_dict(self) -> dict:
        return {
            "caseId": self.case_id,
            "requestId": self.request_id,
            "callIndex": self.call_index,
            "adapterName": self.adapter_name,
            "provider": self.provider,
            "model": self.model,
            "classification": self.classification,
            "method": self.method,
            "callEmitted": self.call_emitted,
            "outputEmitted": self.output_emitted,
            "httpStatus": self.http_status,
            "latencyMs": self.latency_ms,
            "errorType": self.error_type,
            "errorMessage": self.error_message,
            "promptChars": self.prompt_chars,
            "promptSha256": self.prompt_sha256,
            "estimatedPromptTokens": self.estimated_prompt_tokens,
            "messageRoles": list(self.message_roles),
            "systemChars": self.system_chars,
            "outputChars": self.output_chars,
            "rawOutput": self.raw_output,
            "rawOutputTruncated": self.raw_output_truncated,
            "tokenUsage": self.token_usage,
            "tokenUsageNote": self.token_usage_note,
            "streamDiagnostics": self.stream_diagnostics,
            "completionDiagnostics": self.completion_diagnostics,
        }


class RecordingLLMAdapter:
    """透明包装任意 LLMAdapter；``name`` 保持原值以不改变分析器分流。"""

    def __init__(
        self,
        inner: LLMAdapter,
        *,
        case_id: str,
        provider: str,
        model: str,
    ) -> None:
        self._inner = inner
        self._case_id = case_id
        self._provider = provider
        self._model = model
        self.records: list[LLMCallRecord] = []

    @property
    def name(self) -> str:
        # 必须透传：LearningAnalyzer 用 name == "mock" 决定走确定性启发式。
        return self._inner.name

    @property
    def classification(self) -> str:
        if self._provider == "openai":
            return "REAL_LLM"
        return "MOCK"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        record = self._start("stream_chat", messages)
        chunks: list[str] = []
        started = time.perf_counter()
        try:
            async for delta in self._inner.stream_chat(
                messages, temperature=temperature, max_tokens=max_tokens
            ):
                if delta:
                    record.output_emitted = True
                    chunks.append(delta)
                    yield delta
        except Exception as exc:
            self._finish_error(record, exc)
            raise
        finally:
            self._finish(record, chunks, started)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        record = self._start("complete", messages)
        started = time.perf_counter()
        try:
            text = await self._inner.complete(
                messages, temperature=temperature, max_tokens=max_tokens
            )
            record.output_emitted = bool(text)
            return text
        except Exception as exc:
            self._finish_error(record, exc)
            raise
        finally:
            self._finish(record, [text] if "text" in locals() and text else [], started)

    async def aclose(self) -> None:
        await self._inner.aclose()

    # ---------- 内部 ----------

    def _start(self, method: str, messages: list[ChatMessage]) -> LLMCallRecord:
        prompt = "\n".join(message.content for message in messages)
        record = LLMCallRecord(
            case_id=self._case_id,
            request_id=uuid.uuid4().hex,
            call_index=len(self.records) + 1,
            adapter_name=self._inner.name,
            provider=self._provider,
            model=self._model,
            classification=self.classification,
            method=method,
            call_emitted=True,
            prompt_chars=len(prompt),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            estimated_prompt_tokens=estimate_tokens(prompt),
            message_roles=[message.role for message in messages],
            system_chars=len(messages[0].content) if messages else 0,
        )
        self.records.append(record)
        return record

    def _finish_error(self, record: LLMCallRecord, exc: Exception) -> None:
        record.error_type = type(exc).__name__
        record.error_message = redact(str(exc))[:500]
        status = getattr(self._inner, "last_status_code", None)
        record.http_status = status if isinstance(status, int) else record.http_status
        if isinstance(exc, LLMError) and getattr(exc, "status_code", None):
            record.http_status = exc.status_code
        self._capture_diagnostics(record)

    def _capture_diagnostics(self, record: LLMCallRecord) -> None:
        # complete 与 stream_chat 的诊断是两份独立事实；混用会把上一次流式
        # 调用的状态写进本次非流式记录。
        if record.method == "complete":
            diagnostics = getattr(self._inner, "last_completion_diagnostics", None)
            as_dict = getattr(diagnostics, "as_dict", None)
            if callable(as_dict):
                record.completion_diagnostics = as_dict()
            usage = getattr(diagnostics, "usage", None)
            note = "provider usage（非流式响应的 usage 字段）"
        else:
            diagnostics = getattr(self._inner, "last_diagnostics", None)
            as_dict = getattr(diagnostics, "as_dict", None)
            if callable(as_dict):
                record.stream_diagnostics = as_dict()
            usage = getattr(diagnostics, "usage", None)
            note = "provider usage（流式 chunk 的 usage 字段）"
        if isinstance(usage, dict):
            record.token_usage = usage
            record.token_usage_note = note

    def _finish(
        self, record: LLMCallRecord, chunks: list[str], started: float
    ) -> None:
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        status = getattr(self._inner, "last_status_code", None)
        if isinstance(status, int):
            record.http_status = status
        raw = "".join(chunks)
        record.output_chars = len(raw)
        record.raw_output_truncated = len(raw) > MAX_RAW_OUTPUT
        record.raw_output = redact(raw[:MAX_RAW_OUTPUT]) if raw else None
        self._capture_diagnostics(record)


class SequenceLLMAdapter:
    """按调用次数返回固定文本；用于「第一次不合规、重生成合规」的 Guard 用例。"""

    def __init__(self, responses: list[str]) -> None:
        if not responses:
            raise ValueError("SequenceLLMAdapter 至少需要一条响应")
        self._responses = list(responses)
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted-sequence"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        response = self._next()
        for index in range(0, len(response), 40):
            yield response[index : index + 40]

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self._next()

    async def aclose(self) -> None:
        return None

    def _next(self) -> str:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]
