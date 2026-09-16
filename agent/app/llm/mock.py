"""Mock LLM —— 确定性假输出，让整条链路在没有任何 API Key 时也能跑通。

它**不假装有智能**，也不代表真实模型的行为。它做一件具体的事：
把系统提示里组装好的认知背景回读出来，这样你一眼就能看出
「Retriever 检索到了什么 → Context Builder 塞了什么 → Tutor 发出去的是什么」。
换句话说，它是流水线的显影液，不是教学能力。

因此它同时承担两个角色的职责：
- 本地开发 / 离线演示；
- 测试断言 —— 「上下文里的个性化信息确实进了 prompt」这件事可以被它证明。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.llm.base import ChatMessage

# 回读背景时最多列几条，避免 mock 输出长得像一份报告
_DIGEST_LIMIT = 8
# 模拟流式的分块大小（按字符）
_CHUNK_SIZE = 18


class MockLLMAdapter:
    def __init__(self, *, chunk_size: int = _CHUNK_SIZE) -> None:
        self._chunk_size = max(1, chunk_size)

    @property
    def name(self) -> str:
        return "mock"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        reply = self._render(messages)
        for index in range(0, len(reply), self._chunk_size):
            yield reply[index : index + self._chunk_size]

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self._render(messages)

    async def aclose(self) -> None:
        return None

    # ---------- 内部 ----------

    def _render(self, messages: list[ChatMessage]) -> str:
        user_message = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        system_prompt = next((message.content for message in messages if message.role == "system"), "")
        digest = self._digest(system_prompt)

        lines = [
            "（Mock LLM 输出）我收到的问题是：",
            f"> {user_message.strip() or '（空）'}",
            "",
        ]
        if digest:
            lines += [
                "这次我实际拿到的学习背景是：",
                *[f"- {item}" for item in digest],
                "",
                "如果我是一个真实模型，我会接着上面这些已有认知讲，"
                "而不是从零重新解释一遍。",
            ]
        else:
            lines += [
                "这次没有检索到任何历史认知，"
                "说明这是一个新概念，或者 Retriever 没有命中。",
            ]
        lines += [
            "",
            "（这是 mock 适配器，用于离线跑通链路。"
            "把 AILS_LLM_PROVIDER 设成 openai 并配好 Key，"
            "这里会换成真实模型的回答。）",
        ]
        return "\n".join(lines)

    @staticmethod
    def _digest(system_prompt: str, limit: int = _DIGEST_LIMIT) -> list[str]:
        """从系统提示里摘出背景条目，用来证明上下文确实被组装进去了。

        只看第一个 `## ` 之后的内容 —— 之前是教学契约（BASE_SYSTEM_INSTRUCTION），
        里面也有 bullet，但那是写给模型的规则，不是这个学习者的资料。
        """
        body = _background_blocks(system_prompt)
        items: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if item and item not in items:
                items.append(item)
            if len(items) >= limit:
                break
        return items


def _background_blocks(system_prompt: str) -> str:
    """取第一个二级标题之后的部分；没有标题说明这次没检索到背景。"""
    marker = system_prompt.find("\n## ")
    return system_prompt[marker:] if marker >= 0 else ""
