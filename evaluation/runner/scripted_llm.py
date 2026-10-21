"""Recorded LLM 适配器。

评测「解析与收敛」时，不能指望真实模型每次都输出同一段文本 —— 那样用例会随
模型版本漂移，失败也无法判断是代码回归还是模型换了措辞。因此这里把一段
**固定的模型原始输出**喂给真实的分析器：

```text
request → 检索 → prompt（真实代码）
                     ↓
              这段固定文本（代替模型）
                     ↓
        _extract_json_object → sanitize → AnalyzeResult（真实代码）
```

被替换的只有「模型说了什么」，解析、裁剪、conceptId 校验、枚举收敛全部是真的。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.llm.base import ChatMessage


class ScriptedLLMAdapter:
    """每次都返回同一段文本；`name != "mock"`，所以分析器走结构化 JSON 路径。"""

    def __init__(self, response: str) -> None:
        self._response = response

    @property
    def name(self) -> str:
        return "scripted"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # 分析链路只用 complete()；保留一个可用的流式实现，避免误用时抛 AttributeError。
        yield self._response

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self._response

    async def aclose(self) -> None:
        return None
