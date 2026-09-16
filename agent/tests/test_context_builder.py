"""Context Builder 的行为约定。

这些断言回答的是「上下文有没有被正确地组织」：谁优先、谁被丢、丢的时候是否留痕、
最终 messages 的结构是否合法。它们不关心模型怎么回答 —— 那是 Mock 与评测集的事。
"""

from __future__ import annotations

from app.context.builder import ContextBuilder
from app.schemas import (
    EvidenceCorrectness,
    EvidenceItem,
    HistoryMessage,
    KnowledgeChunk,
    MemoryItem,
    MemoryType,
    MessageRole,
    Misconception,
    MisconceptionStatus,
    Understanding,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace

UNDERSTANDING = Understanding(
    id=500,
    concept_id=10,
    concept_name="TLB",
    summary="用户理解 TLB 是页表项的缓存。",
    known_parts=["TLB 是页表项的缓存", "TLB Hit 与 TLB Miss 的区别"],
    misconceptions=[
        Misconception(
            text="把 TLB Miss 与 Page Fault 混为一谈",
            status=MisconceptionStatus.ACTIVE,
        )
    ],
    uncertain_parts=["Page Table 本身存放在哪里"],
    latest_user_explanation="它应该就是 Page Table 的缓存。",
    status=UnderstandingStatus.MISCONCEPTION,
)


def make_state(message: str = "TLB Miss 是什么？", **overrides) -> AgentState:
    state = AgentState(
        run_id=1,
        user_id=1,
        conversation_id=1,
        space_id=1,
        user_message=message,
        trace=RunTrace(run_id=1),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def make_memory(memory_id: int = 100, content: str = "用户喜欢从硬件执行路径理解 OS。") -> MemoryItem:
    return MemoryItem(id=memory_id, memory_type=MemoryType.PREFERENCE, content=content)


def test_messages_shape_is_system_then_conversation_then_current_input():
    state = make_state(
        "那 TLB Miss 之后呢？",
        history=[
            HistoryMessage(role=MessageRole.USER, content="TLB 是什么？"),
            HistoryMessage(role=MessageRole.ASSISTANT, content="TLB 是页表项缓存。"),
        ],
        understandings=[UNDERSTANDING],
    )

    context = ContextBuilder().build(state)

    assert context.messages[0].role == "system"
    assert context.messages[-1].role == "user"
    assert context.messages[-1].content == "那 TLB Miss 之后呢？"
    assert [message.role for message in context.messages[1:-1]] == ["user", "assistant"]


def test_understanding_misconception_reaches_the_model():
    context = ContextBuilder().build(make_state(understandings=[UNDERSTANDING]))
    system = context.messages[0].content

    assert "TLB" in system
    assert "把 TLB Miss 与 Page Fault 混为一谈" in system
    assert "当前存在" in system
    assert "它应该就是 Page Table 的缓存。" in system


def test_unknown_empty_snapshot_is_not_injected():
    """UNKNOWN 且什么都不含的快照不提供信息，不该占预算。"""
    empty = Understanding(id=9, concept_id=11, concept_name="Page Table")
    context = ContextBuilder().build(make_state(understandings=[empty]))

    assert "Page Table" not in context.messages[0].content
    assert any("没有检索到任何 Understanding" in warning for warning in context.warnings)


def test_no_retrieval_result_says_so_explicitly():
    context = ContextBuilder().build(make_state())

    assert "没有检索到任何历史认知记录" in context.messages[0].content


def test_history_duplicate_of_current_input_is_dropped():
    state = make_state(
        "TLB 是什么？",
        history=[HistoryMessage(role=MessageRole.USER, content="TLB 是什么？")],
    )

    context = ContextBuilder().build(state)
    user_messages = [message for message in context.messages if message.role == "user"]

    assert len(user_messages) == 1


def test_budget_drops_low_priority_sections_first():
    state = make_state(
        understandings=[UNDERSTANDING],
        memories=[make_memory()],
        knowledge=[
            KnowledgeChunk(id=200, concept_id=10, content="TLB 是 MMU 内部的页表项缓存。" * 20),
        ],
        evidences=[
            EvidenceItem(id=300, concept_id=10, observation="用户答错了", correctness=EvidenceCorrectness.INCORRECT),
        ],
        history=[HistoryMessage(role=MessageRole.ASSISTANT, content="很长的历史消息。" * 30)],
    )

    context = ContextBuilder(max_context_tokens=200).build(state)

    assert "knowledge:200" in context.dropped_sections
    assert "user_input" not in context.dropped_sections
    assert any("超出 Token 预算被丢弃" in warning for warning in context.warnings)


def test_current_input_survives_even_when_it_alone_exceeds_budget():
    """模型看不到问题，这次回答就没有意义 —— 所以本轮输入永不丢弃。"""
    state = make_state("TLB " * 500)

    context = ContextBuilder(max_context_tokens=50).build(state)

    assert context.messages[-1].content.strip()
    assert "user_input" not in context.dropped_sections
    user_input = next(section for section in context.sections if section.name == "user_input")
    assert user_input.truncated


def test_recent_history_wins_over_older_history():
    history = [HistoryMessage(role=MessageRole.USER, content=f"第{i}轮" + "长" * 60) for i in range(6)]
    state = make_state(history=history)

    context = ContextBuilder(max_context_tokens=120).build(state)
    kept = [section for section in context.sections if section.name.startswith("history:") and not section.dropped]

    # 被保留下来的必须是最新的那几轮
    assert kept
    assert max(int(section.name.split(":")[1]) for section in kept) == len(history)


def test_sections_expose_their_sources_for_debugging():
    context = ContextBuilder().build(
        make_state(understandings=[UNDERSTANDING], memories=[make_memory()])
    )

    sources = {source for section in context.sections for source in section.source_ids}
    assert "understanding:500" in sources
    assert "memory:100" in sources


def test_preview_payload_is_json_serialisable():
    import json

    context = ContextBuilder().build(make_state(understandings=[UNDERSTANDING]))
    json.dumps(context.as_dict(), ensure_ascii=False)
