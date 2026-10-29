"""Context Builder 的行为约定。

这些断言回答的是「上下文有没有被正确地组织」：谁优先、谁被丢、丢的时候是否留痕、
最终 messages 的结构是否合法。它们不关心模型怎么回答 —— 那是 Mock 与评测集的事。
"""

from __future__ import annotations

from datetime import datetime

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
    SessionContext,
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


def test_session_goal_and_focus_reach_the_model():
    """本阶段学习目标要进 prompt —— 这是「新开一个对话接着学同一个目标」能接上的前提。"""
    state = make_state(
        session=SessionContext(
            id=20,
            title="虚拟内存地址转换",
            goal="理解 VA → TLB → Page Table → PA",
            current_focus="卡在 TLB Miss 与 Page Fault 的关系",
        )
    )

    system = ContextBuilder().build(state).messages[0].content

    assert "理解 VA → TLB → Page Table → PA" in system
    assert "卡在 TLB Miss 与 Page Fault 的关系" in system


def test_missing_session_adds_no_section():
    """session 为 None 是常态（懒创建），不该为它渲染一个「未设置」的占位区块。"""
    context = ContextBuilder().build(make_state())

    assert not any(section.name == "session" for section in context.sections)
    assert "本阶段学习目标" not in context.messages[0].content


def test_session_without_goal_or_focus_is_skipped():
    """只有 id 的空 Session 提供不了信息，跳过而不是渲染一个空标题。"""
    context = ContextBuilder().build(make_state(session=SessionContext(id=20)))

    assert not any(section.name == "session" for section in context.sections)


def test_session_section_survives_history_pressure():
    """目标区块很短，长对话里被挤掉历史时它应当在 —— 它是整轮回复的框架。"""
    history = [HistoryMessage(role=MessageRole.USER, content=f"第{i}轮" + "长" * 60) for i in range(8)]
    state = make_state(
        history=history,
        session=SessionContext(id=20, goal="理解虚拟内存地址转换"),
    )

    context = ContextBuilder(max_context_tokens=120).build(state)

    assert "理解虚拟内存地址转换" in context.messages[0].content


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


def test_knowledge_source_label_is_traceable_document_name_and_chunk_index():
    chunk = KnowledgeChunk(
        id=200,
        document_id=1,
        chunk_index=3,
        document_name="操作系统笔记.md",
        file_type="md",
        heading_path=["虚拟内存", "TLB"],
        content="TLB 是 MMU 内部的页表项缓存。",
    )
    context = ContextBuilder().build(make_state(knowledge=[chunk]))
    system = context.messages[0].content

    assert "[资料《操作系统笔记.md》#3]" in system
    assert "章节：虚拟内存 > TLB" in system
    # 不要把内部 chunk_id 当作模型可引用的来源名
    assert "资料片段#200" not in system


def test_duplicate_knowledge_chunks_are_rendered_once():
    first = KnowledgeChunk(
        id=200, document_name="notes.md", chunk_index=0, content="同一段内容"
    )
    duplicate = KnowledgeChunk(
        id=200, document_name="notes.md", chunk_index=0, content="同一段内容"
    )

    context = ContextBuilder().build(make_state(knowledge=[first, duplicate]))
    system = context.messages[0].content

    assert system.count("[资料《notes.md》#0]") == 1
    assert len([s for s in context.sections if s.name == "knowledge:200"]) == 1


def test_knowledge_warning_surfaces_and_low_priority_chunk_is_still_droppable():
    state = make_state(
        knowledge=[
            KnowledgeChunk(
                id=200,
                document_name="notes.md",
                chunk_index=0,
                content="检索到的资料。" * 60,
            )
        ],
        knowledge_warning="知识检索依赖不可用，本次回答未使用文档资料。",
    )

    context = ContextBuilder(max_context_tokens=120).build(state)

    assert "knowledge:200" in context.dropped_sections
    assert any("知识检索依赖不可用" in warning for warning in context.warnings)
    assert "知识检索依赖不可用" not in context.messages[0].content


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


def test_no_valid_documents_status_reaches_the_model_without_fake_certainty():
    state = make_state(
        knowledge_status="NO_VALID_DOCUMENTS",
        knowledge_warning="学习空间中没有可用文档。",
    )

    context = ContextBuilder().build(state)
    system = context.messages[0].content

    assert "本轮知识检索状态" in system
    assert "没有可用且已就绪的上传文档" in system
    assert "不要声称「资料中不存在该信息」" in system
    assert "不要编造资料引用" in system
    assert "学习空间中没有可用文档。" in system


def test_threshold_filtered_status_is_not_reported_as_no_such_fact():
    state = make_state(
        knowledge_status="THRESHOLD_FILTERED",
        knowledge_warning="候选片段均低于相关度阈值。",
    )

    system = ContextBuilder().build(state).messages[0].content

    assert "全部低于相关度阈值" in system
    assert "不等同于" in system or "不要把「未检索到」说成「资料中不存在」" in system
    assert "低分候选不能当作依据" in system


def test_degraded_status_says_retrieval_failed_instead_of_no_answer():
    state = make_state(
        knowledge_status="DEGRADED",
        knowledge_warning="知识检索失败，本次回答未使用文档资料。",
    )

    system = ContextBuilder().build(state).messages[0].content

    assert "知识检索不可用" in system
    assert "无法判断上传文档是否包含答案" in system
    assert "不要编造引用" in system


def test_success_status_does_not_add_a_redundant_block():
    state = make_state(
        knowledge_status="SUCCESS",
        knowledge=[KnowledgeChunk(id=201, document_name="notes.md", chunk_index=0, content="事实。")],
    )

    section_names = [section.name for section in ContextBuilder().build(state).sections]
    assert "knowledge_status" not in section_names
    assert "knowledge:201" in section_names


def test_status_survives_budget_drop_of_low_priority_knowledge_chunk():
    state = make_state(
        knowledge_status="THRESHOLD_FILTERED",
        knowledge=[
            KnowledgeChunk(
                id=202,
                document_name="notes.md",
                chunk_index=0,
                content="低分候选。" * 80,
            )
        ],
    )

    context = ContextBuilder(max_context_tokens=150).build(state)

    assert "knowledge:202" in context.dropped_sections
    assert "本轮知识检索状态" in context.messages[0].content


def test_learning_memory_is_marked_as_historical_not_current_belief():
    memory = MemoryItem(
        id=105,
        memory_type=MemoryType.LEARNING,
        content="用户认为 TLB 缓存整个页表",
        created_at=datetime(2026, 10, 27, 0, 46),
    )

    system = ContextBuilder().build(make_state(memories=[memory])).messages[0].content

    assert "学习状态（历史记录）" in system
    assert "用户认为 TLB 缓存整个页表" in system
    assert "记录于 2026-10-27 00:46" in system
    assert "不等于用户此刻的认知" in system


def test_understanding_block_limits_mastery_to_explicit_known_parts():
    system = ContextBuilder().build(
        make_state(understandings=[UNDERSTANDING])
    ).messages[0].content

    assert "只有「已经理解」明确列出的内容才算已掌握" in system
    assert "「部分理解」「接触过」「未接触」都不能说成已掌握" in system


def test_prompt_requires_evidence_bound_history_and_missing_data_disclosure():
    from app.prompts import BASE_SYSTEM_INSTRUCTION

    # 历史复盘只能陈述记录事实，不能补写心理过程。
    assert "你突然意识到" in BASE_SYSTEM_INSTRUCTION
    assert "记录里看不出原因" in BASE_SYSTEM_INSTRUCTION
    # Current-only 没有轨迹时不能否认历史。
    assert "不要因此断言「你以前没有这个错误」" in BASE_SYSTEM_INSTRUCTION
    # 机制类问题要求区分缓存未命中 / 查找 / 异常 / 处理者。
    assert "缓存里有没有命中" in BASE_SYSTEM_INSTRUCTION
    assert "由谁处理这个异常" in BASE_SYSTEM_INSTRUCTION


def test_learning_memory_conflict_is_called_out_next_to_current_understanding():
    memory = MemoryItem(
        id=105,
        concept_id=10,
        memory_type=MemoryType.LEARNING,
        content="用户认为 TLB 缓存整个页表",
        created_at=datetime(2026, 10, 27, 0, 46),
    )

    system = ContextBuilder().build(
        make_state(understandings=[UNDERSTANDING], memories=[memory])
    ).messages[0].content

    assert "长期记忆里另有一条较早的学习状态记录" in system
    assert "当前状态已不同" in system
    assert "不要把它当成本轮事实" in system


def test_understanding_block_handles_missing_trajectory_explicitly():
    system = ContextBuilder().build(
        make_state(understandings=[UNDERSTANDING])
    ).messages[0].content

    assert "如果本轮没有提供历史轨迹" in system
    assert "不要从当前状态反推过去" in system
    assert "没有看到错误记录" in system


# ---------- Phase 4.1：检索状态 vs 预算后的最终状态 ----------


def test_success_with_injected_knowledge_stays_success():
    chunk = KnowledgeChunk(
        id=210,
        document_name="orbit_current.md",
        chunk_index=0,
        content="ORBIT-4721 当前调度器使用 17 秒的重试窗口。",
    )
    context = ContextBuilder().build(
        make_state(
            "ORBIT-4721 当前重试窗口是多少？",
            knowledge_status="SUCCESS",
            knowledge=[chunk],
        )
    )

    assert context.knowledge_status == "SUCCESS"
    assert context.knowledge_state == "SUCCESS"
    assert context.knowledge_injected_chunk_ids == [210]
    assert context.knowledge_dropped_chunk_ids == []
    assert "knowledge_status" not in [section.name for section in context.sections]
    assert "[资料《orbit_current.md》#0]" in context.messages[0].content


def test_success_but_all_knowledge_dropped_is_not_reported_as_no_match():
    chunk = KnowledgeChunk(
        id=211,
        document_name="orbit_current.md",
        chunk_index=0,
        content="ORBIT-4721 当前调度器使用 17 秒的重试窗口。" * 40,
    )
    context = ContextBuilder(max_context_tokens=60).build(
        make_state(
            "ORBIT-4721 当前重试窗口是多少？",
            knowledge_status="SUCCESS",
            knowledge=[chunk],
        )
    )

    assert context.knowledge_state == "SUCCESS_BUT_DROPPED"
    assert context.knowledge_injected_chunk_ids == []
    assert context.knowledge_dropped_chunk_ids == [211]

    system = context.messages[0].content
    assert "SUCCESS_BUT_DROPPED" in system
    assert "检索成功" in system
    assert "全部因上下文 Token 预算未能放入本次 Prompt" in system
    assert "不等于「没有检索到资料」" in system
    # 不能退化成「没有可用文档 / 没有命中」的说明。
    assert "没有可用且已就绪的上传文档" not in system
    assert "没有找到可用的文档片段" not in system
    assert any("知识检索成功，但全部知识片段" in warning for warning in context.warnings)


def test_partial_drop_reports_counts_and_only_injected_chunks_are_usable():
    injected = KnowledgeChunk(
        id=212,
        document_name="orbit_current.md",
        chunk_index=0,
        content="当前值 17 秒。",
    )
    dropped = KnowledgeChunk(
        id=213,
        document_name="orbit_legacy.md",
        chunk_index=0,
        content="旧调度器使用 12 秒。" * 120,
    )
    context = ContextBuilder(max_context_tokens=300).build(
        make_state(
            "ORBIT-4721 当前重试窗口是多少？",
            knowledge_status="SUCCESS",
            knowledge=[injected, dropped],
        )
    )

    assert context.knowledge_state == "SUCCESS_PARTIAL"
    assert context.knowledge_injected_chunk_ids == [212]
    assert context.knowledge_dropped_chunk_ids == [213]

    system = context.messages[0].content
    assert "SUCCESS_PARTIAL" in system
    assert "1 个已放入本次 Prompt" in system
    assert "1 个因 Token 预算未放入" in system
    assert "只能依据「参考资料」中实际出现的片段" in system
    assert "[资料《orbit_current.md》#0]" in system
    assert "[资料《orbit_legacy.md》#0]" not in system


def test_no_match_keeps_retrieval_state_and_has_no_knowledge_chunks():
    context = ContextBuilder().build(
        make_state("镓的熔点是多少？", knowledge_status="NO_MATCH")
    )

    assert context.knowledge_status == "NO_MATCH"
    assert context.knowledge_state == "NO_MATCH"
    assert context.knowledge_injected_chunk_ids == []
    assert context.knowledge_dropped_chunk_ids == []
    assert "本轮知识检索没有找到可用的文档片段" in context.messages[0].content
