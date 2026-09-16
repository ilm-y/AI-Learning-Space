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
from typing import AsyncIterator, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """模型调用失败。Tutor 会把它翻译成 SSE 的 error 事件，而不是让连接静默断掉。"""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        """流式产出增量文本。实现方负责在结束时自然结束迭代。"""
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
