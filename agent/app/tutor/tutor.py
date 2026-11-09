"""Tutor —— 把组装好的上下文交给模型，产出给用户的那段回答。

架构文档第三节的划分在这里体现得最清楚：

```text
        Learning Agent
              │
   ┌──────────┼──────────┐
   ↓          ↓          ↓
Context     LLM       Tools
Builder              /Services
```

Tutor 是「LLM」那一格。它**不检索、不判断学得怎么样、不更新任何状态** ——
那些是 Retriever 与将来异步学习分析的事。它的职责只有两条：

1. 守住教学契约：发给模型的消息必须满足「一条 system + 按时间序的对话 + 最后是本轮输入」，
   这个不变量由它兜底，而不是指望上游永远正确；
2. 守住流式链路：空流、模型报错都要变成明确的失败，不能推着推着就没声了。

之所以不把它写成一行 `async for delta in llm.stream_chat(...)`，就是这两条兜底：
将来加「过滤内部分析信号」「流式纠正」这类处理时，落点在这里，而不是散在入口。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.context.builder import BuiltContext
from app.llm.base import ChatMessage, LLMAdapter, LLMError, StreamCompletion
from app.prompts import quote_instruction_for

# 模型一个字都没吐出来时，宁可明确报错，也不要给用户一条空消息
EMPTY_REPLY_HINT = "模型没有返回任何内容"


class Tutor:
    def __init__(self, llm: LLMAdapter) -> None:
        self._llm = llm

    @property
    def llm_name(self) -> str:
        return self._llm.name

    async def stream(
        self,
        context: BuiltContext,
        *,
        extra_instruction: str | None = None,
        temperature: float | None = None,
        completion: StreamCompletion | None = None,
    ) -> AsyncIterator[str]:
        """把上下文交给模型。

        `extra_instruction` 只追加到 system message 尾部，用于显式复盘的一次受限
        重生成；它不改变消息结构（仍是一条 system + 对话 + 最后一条 user），
        也不会把上次的错误回答重新喂给模型。

        `completion` 是调用方提供的可变终态槽：适配器支持时，流正常耗尽后在这里
        写入 ``finish_reason``，供 Runtime 在最终 SSE 事件里标记截断。适配器没提供
        终态方法时保持 ``metadata_available=False``，不推断截断。
        """
        messages = self._validated(context.messages)
        messages = self._apply_request_instruction(messages)
        if extra_instruction:
            messages = [
                ChatMessage("system", f"{messages[0].content}\n\n{extra_instruction}"),
                *messages[1:],
            ]
        stream = self._open_stream(messages, temperature=temperature)
        produced = False
        async for delta in stream:
            if not delta:
                continue
            produced = True
            yield delta
        if completion is not None:
            terminal = getattr(stream, "completion", None)
            if isinstance(terminal, StreamCompletion):
                completion.copy_from(terminal)
        if not produced:
            raise LLMError(EMPTY_REPLY_HINT)

    def _open_stream(self, messages: list, *, temperature: float | None):
        """优先使用带终态元数据的流；旧适配器退回纯文本流。"""
        opener = getattr(self._llm, "stream_chat_with_completion", None)
        if callable(opener):
            return opener(messages, temperature=temperature)
        return self._llm.stream_chat(messages, temperature=temperature)

    async def complete(self, context: BuiltContext) -> str:
        """一次性产出。给不需要流式的场景（将来的结构化任务）留的入口。"""
        messages = self._apply_request_instruction(self._validated(context.messages))
        return await self._llm.complete(messages)

    @staticmethod
    def _apply_request_instruction(messages: list) -> list:
        """按本轮用户输入条件追加约束，不改动 ContextBuilder 产出的内容。

        引用类请求（「引用 / 原话 / 原文 / 原句 / 复述」）在 system message
        末尾追加逐字引用约束；其他请求原样返回，system prompt 与改动前逐字
        一致。全局 BASE_SYSTEM_INSTRUCTION 本身不被修改。
        """
        instruction = quote_instruction_for(messages[-1].content)
        if not instruction:
            return messages
        return [
            ChatMessage("system", f"{messages[0].content}\n\n{instruction}"),
            *messages[1:],
        ]

    @staticmethod
    def _validated(messages: list) -> list:
        if not messages:
            raise LLMError("上下文为空，没有可发送的消息")
        if messages[0].role != "system":
            raise LLMError("消息序列的第一条必须是 system instruction")
        if messages[-1].role != "user":
            raise LLMError("消息序列的最后一条必须是本轮用户输入")
        return messages
