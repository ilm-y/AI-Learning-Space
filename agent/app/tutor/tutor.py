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
from app.llm.base import LLMAdapter, LLMError

# 模型一个字都没吐出来时，宁可明确报错，也不要给用户一条空消息
EMPTY_REPLY_HINT = "模型没有返回任何内容"


class Tutor:
    def __init__(self, llm: LLMAdapter) -> None:
        self._llm = llm

    @property
    def llm_name(self) -> str:
        return self._llm.name

    async def stream(self, context: BuiltContext) -> AsyncIterator[str]:
        messages = self._validated(context.messages)
        produced = False
        async for delta in self._llm.stream_chat(messages):
            if not delta:
                continue
            produced = True
            yield delta
        if not produced:
            raise LLMError(EMPTY_REPLY_HINT)

    async def complete(self, context: BuiltContext) -> str:
        """一次性产出。给不需要流式的场景（将来的结构化任务）留的入口。"""
        return await self._llm.complete(self._validated(context.messages))

    @staticmethod
    def _validated(messages: list) -> list:
        if not messages:
            raise LLMError("上下文为空，没有可发送的消息")
        if messages[0].role != "system":
            raise LLMError("消息序列的第一条必须是 system instruction")
        if messages[-1].role != "user":
            raise LLMError("消息序列的最后一条必须是本轮用户输入")
        return messages
