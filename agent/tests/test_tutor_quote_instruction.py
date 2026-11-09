"""引用类请求的逐字引用约束：只在正确的请求上追加，且不改动全局契约。

约束的目标是修复 S5-B 的模型行为不稳定（引用了 Understanding 转述而不是
历史原句）。测试守住三条边界：

1. 引用类请求：system message 末尾追加固定约束；
2. 非引用类请求：system message 与改动前逐字一致；
3. 全局 ``BASE_SYSTEM_INSTRUCTION`` 本身不含该约束，也不被修改。
"""

from __future__ import annotations

import pytest

from app.context.builder import BuiltContext
from app.llm.base import ChatMessage
from app.prompts import (
    BASE_SYSTEM_INSTRUCTION,
    QUOTE_REQUEST_KEYWORDS,
    VERBATIM_QUOTE_INSTRUCTION,
    is_quote_request,
    quote_instruction_for,
)
from app.tutor.tutor import Tutor


class RecordingAdapter:
    """记录实际发给模型的消息，并返回固定文本。"""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(self, messages, *, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        yield "回答"

    async def complete(self, messages, *, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        return "回答"

    async def aclose(self) -> None:
        return None


def _context(user_text: str) -> BuiltContext:
    return BuiltContext(
        system_instruction=BASE_SYSTEM_INSTRUCTION,
        messages=[
            ChatMessage("system", BASE_SYSTEM_INSTRUCTION),
            ChatMessage("user", user_text),
        ],
        sections=[],
        estimated_tokens=0,
    )


async def _streamed_messages(user_text: str, **kwargs) -> list[ChatMessage]:
    adapter = RecordingAdapter()
    tutor = Tutor(adapter)
    async for _ in tutor.stream(_context(user_text), **kwargs):
        pass
    return adapter.calls[0]


def test_global_contract_does_not_contain_the_conditional_instruction():
    assert VERBATIM_QUOTE_INSTRUCTION not in BASE_SYSTEM_INSTRUCTION


def test_instruction_defines_each_source_and_only_history_counts_as_verbatim():
    text = VERBATIM_QUOTE_INSTRUCTION
    sources = ("history", "Understanding", "Memory", "Evidence")
    for source in sources:
        assert source in text, source
    # 只有 history 可以被称为「原话」；其余三类必须明确写成转述 / 非原话。
    assert "history 是用户逐字说过的话，只有它可以称为「原话」" in text
    assert "Understanding 是系统对用户当前掌握状态的总结，是转述，不是原话" in text
    assert "Memory 是系统提炼的长期信息，不是原话" in text
    assert "Evidence 是系统记录的学习事件，不是原话" in text


def test_instruction_separates_history_and_understanding_labels():
    text = VERBATIM_QUOTE_INSTRUCTION
    assert "history 的原句必须标注「你当时说：」" in text
    assert "Understanding 的内容必须标注「系统记录的你当前理解：」" in text
    assert "不得把 Understanding 称为「你更早的说法」「另一句原话」" in text


def test_quote_request_detection_covers_all_keywords():
    for keyword in QUOTE_REQUEST_KEYWORDS:
        assert is_quote_request(f"请{keyword}一下"), keyword
        assert quote_instruction_for(f"请{keyword}一下") == VERBATIM_QUOTE_INSTRUCTION


@pytest.mark.parametrize(
    "user_text",
    [
        "解释一下 TCP 慢启动为什么叫慢启动",
        "我现在的薄弱点是什么",
        "继续讲拥塞避免",
        "",
    ],
)
def test_non_quote_requests_are_not_detected(user_text):
    assert is_quote_request(user_text) is False
    assert quote_instruction_for(user_text) is None


@pytest.mark.parametrize("keyword", QUOTE_REQUEST_KEYWORDS)
async def test_quote_request_appends_instruction_at_system_tail(keyword):
    messages = await _streamed_messages(f"请{keyword}我当时的说法")

    assert [message.role for message in messages] == ["system", "user"]
    assert messages[0].content == (
        f"{BASE_SYSTEM_INSTRUCTION}\n\n{VERBATIM_QUOTE_INSTRUCTION}"
    )
    # 用户消息与上下文内容不被改写。
    assert messages[1].content == f"请{keyword}我当时的说法"


async def test_non_quote_request_keeps_system_prompt_byte_identical():
    messages = await _streamed_messages("解释一下 TCP 慢启动为什么叫慢启动")

    assert messages[0].content == BASE_SYSTEM_INSTRUCTION
    assert VERBATIM_QUOTE_INSTRUCTION not in messages[0].content


async def test_guard_extra_instruction_is_appended_after_quote_instruction():
    messages = await _streamed_messages(
        "请引用我当时的原话",
        extra_instruction="纠正：不要声称记录里有两条。",
    )

    assert messages[0].content.endswith(
        f"{VERBATIM_QUOTE_INSTRUCTION}\n\n纠正：不要声称记录里有两条。"
    )


async def test_complete_uses_the_same_conditional_instruction():
    adapter = RecordingAdapter()
    tutor = Tutor(adapter)

    await tutor.complete(_context("请复述我当时的原话"))
    await tutor.complete(_context("解释一下拥塞窗口"))

    assert adapter.calls[0][0].content.endswith(VERBATIM_QUOTE_INSTRUCTION)
    assert adapter.calls[1][0].content == BASE_SYSTEM_INSTRUCTION
