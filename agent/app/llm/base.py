"""LLM Adapter —— Agent 与「模型」之间的唯一界面。

对应 `agent-architecture.md` 第三节的核心原则：

> **LLM ≠ Agent。** LLM 只负责理解输入与生成文本，Agent 负责什么时候检索、
> 检索什么、什么时候更新状态。

所以这一层被刻意做薄：它只认 messages 与 token 参数，不认识 Understanding、
Memory、Tutor，也不认识任何业务概念。换模型厂商不需要动 Context Builder 与 Tutor。

两个能力：
- `stream_chat` —— 流式生成，Tutor 用；这是用户等待的那条链路。
- `complete`    —— 一次性生成，将来 Evidence 抽取 / 简答判分这类**结构化输出**用它，
                  本轮还没有调用方，先留在协议里（见 agent/README 的进度表）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """模型调用失败。Tutor 会把它翻译成 SSE 的 error 事件，而不是让连接静默断掉。"""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class StreamCompletion:
    """一次流式生成的终态事实。

    适配器负责把供应商的 ``finish_reason`` 归一化后放进这里。``finish_reason``
    为 ``length`` 表示模型因为 Token 上限停止，已经产出的内容是不完整的；
    ``metadata_available=False`` 表示适配器没有提供终态信息（例如测试替身），
    此时只能按旧语义当作正常结束，不能凭空推断截断。
    """

    finish_reason: str | None = None
    metadata_available: bool = False

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def copy_from(self, other: StreamCompletion) -> None:
        self.finish_reason = other.finish_reason
        self.metadata_available = other.metadata_available


class CompletionAwareStream:
    """把普通文本流包成「耗尽后可读取终态」的迭代器。

    只在正常耗尽时调用 ``completion_factory``。流提前被丢弃或抛出异常时
    ``completion`` 保持 ``None`` —— 调用方不能把「没读完」误当成「正常结束」。
    """

    def __init__(
        self,
        inner: AsyncIterator[str],
        completion_factory: Callable[[], StreamCompletion],
    ) -> None:
        self._inner = inner
        self._completion_factory = completion_factory
        self._completion: StreamCompletion | None = None
        self._exhausted = False

    def __aiter__(self) -> CompletionAwareStream:
        return self

    async def __anext__(self) -> str:
        if self._exhausted:
            raise StopAsyncIteration
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            self._completion = self._completion_factory()
            raise

    @property
    def completion(self) -> StreamCompletion | None:
        """正常耗尽后的终态；未读完或中途失败时为 ``None``。"""
        return self._completion


@dataclass(slots=True)
class ChatMessage:
    """一条发给模型的消息。`role` 用 OpenAI 的取值：system / user / assistant。"""

    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@runtime_checkable
class LLMAdapter(Protocol):
    """所有 LLM 适配器都要满足的协议。"""

    @property
    def name(self) -> str:
        """适配器标识，进日志与 /health。"""

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式产出增量文本。实现方负责在结束时自然结束迭代。

        若适配器能拿到供应商的 ``finish_reason``，应额外实现
        ``stream_chat_with_completion(...) -> CompletionAwareStream``。
        Tutor 会优先使用它，把 ``StreamCompletion`` 传给 Runtime；没有该方法时
        退回本方法，并按「终态未知」处理，保持旧适配器可用。
        """
        ...

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """一次性返回完整文本。"""
        ...

    async def aclose(self) -> None:
        """释放底层连接。"""
        ...
