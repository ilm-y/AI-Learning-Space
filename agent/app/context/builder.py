"""Context Builder —— 把检索结果变成一次真正发给模型的上下文。

对应 `agent-architecture.md` 第六、七节。它存在的理由是那句「不能 SELECT * 然后
全部塞进 Prompt」：检索回来的东西是**候选**，不是上下文。这一层负责五件事：

```text
1. 去重      同一个 Concept 的快照 / 同一条 Memory，只留一份
2. 排优先级  Current Input > Conversation > Understanding > Memory > Knowledge > Evidence
3. 控 Token  超预算时从最低优先级开始丢，丢掉的要能被看见（dropped）
4. 处理冲突  当前用户明确表达 > 历史 Memory，这条规则写进 System Instruction
5. 组织结构  按固定区块渲染，让模型知道哪一段是什么、能信到什么程度
```

产物分两部分，这个划分很重要：

- `system_instruction` —— 认知背景（Understanding / Memory / Knowledge / Evidence），
  是「关于这个用户的资料」，模型参考它，但**不应该复述它**；
- `messages` —— 真正的对话（历史 + 本轮输入），这是模型要回应的话。

`BuiltContext.sections` 把每一段的来源、Token、是否被裁剪都摊开，
`POST /agent/context/preview` 直接把它回给人看 —— 「这次到底喂了什么给模型」
不应该是一个只能靠猜的问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.citations import format_source_label
from app.context.tokens import estimate_tokens, truncate_to_tokens
from app.context.trajectory import (
    historical_misconceptions,
    misconception_first_seen,
    misconception_last_status,
)
from app.llm.base import ChatMessage
from app.prompts import BASE_SYSTEM_INSTRUCTION
from app.schemas import (
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceType,
    KnowledgeChunk,
    LearningTrajectory,
    MemoryItem,
    MemoryScopeType,
    MemoryType,
    MisconceptionStatus,
    SessionContext,
    Understanding,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState

# ---------- 优先级：数字越大越先保留 ----------
# 顺序照抄 agent-architecture.md 第六节，不要随手调整。
PRIORITY_USER_INPUT = 100
PRIORITY_HISTORY = 90
# 学习目标：一到两行的框定信息，很便宜。排在历史之后、认知背景之前 ——
# 丢了它只是少一层「我们正在做什么」的框架，丢了 Understanding 才是丢了「你之前是怎么理解的」。
PRIORITY_SESSION = 85
PRIORITY_UNDERSTANDING = 70
# 历史轨迹低于当前理解：复盘要能看到过去，但当前状态永远先被保留。
PRIORITY_TRAJECTORY = 68
PRIORITY_MEMORY = 50
# 知识检索状态比知识片段本身略高：即使片段全部被预算丢掉，模型也必须知道
# 「这次是没有可用文档 / 阈值过滤 / 检索降级」，而不是把空知识区当成「资料里没有」。
PRIORITY_KNOWLEDGE_STATUS = 45
PRIORITY_KNOWLEDGE = 40
PRIORITY_EVIDENCE = 20
# 跨概念扩展证据优先级更低：它是「相关概念的历史参考」，永远不能挤掉
# 当前概念自己的判定依据。
PRIORITY_RELATED_EVIDENCE = 18

# 渲染进 System Instruction 时的区块顺序（与优先级无关：优先级决定谁被丢，
# 顺序决定人读起来顺不顺）。session 放最前：它是后面一切内容的框架。
RENDER_ORDER = (
    "session",
    "understanding",
    "trajectory",
    "memory",
    "knowledge_status",
    "knowledge",
    "evidence",
    "related_evidence",
)

# 检索层状态描述「检索发生了什么」；预算裁剪后的状态描述「模型最终看到了什么」。
# 两者不能混为一谈：SUCCESS 只说明检索成功，不保证片段最后仍在 Prompt 里。
KNOWLEDGE_STATE_SUCCESS = "SUCCESS"
KNOWLEDGE_STATE_SUCCESS_PARTIAL = "SUCCESS_PARTIAL"
KNOWLEDGE_STATE_SUCCESS_BUT_DROPPED = "SUCCESS_BUT_DROPPED"

# 只有检索结果不是 SUCCESS 时才需要向模型解释「为什么没有知识片段」。
# 每一种状态的措辞都必须区分「没有可用文档」「没有检索到支持」「检索失败无法确认」，
# 不能让模型把「这次没检索到」说成「资料或世界上不存在」。
_KNOWLEDGE_STATUS_NOTES = {
    "NO_VALID_DOCUMENTS": (
        "本轮没有可用且已就绪的上传文档（空间为空或文档尚未索引完成）。"
        "你无法判断文档里是否有答案；不要声称「资料中不存在该信息」，也不要编造资料引用。"
        "如果回答，先说明没有可用资料，再明确区分通用知识。"
    ),
    "NO_MATCH": (
        "本轮知识检索没有找到可用的文档片段（没有命中）。"
        "这不等同于该信息在资料或世界上不存在。回答文档相关问题时，"
        "先说明上传资料中没有检索到直接依据；不要编造引用。通用知识必须标明为通用知识。"
        "即使这个问题可以用通用知识回答，也必须先在回答里明确说明"
        "「本轮上传资料中没有检索到可确认的依据」，再区分通用知识。"
    ),
    "THRESHOLD_FILTERED": (
        "本轮知识检索的候选片段全部低于相关度阈值，未作为可靠证据使用。"
        "低分候选不能当作依据；请说明没有检索到可确认的资料依据，"
        "不要编造引用，也不要把「未检索到」说成「资料中不存在」。"
        "即使这个问题可以用通用知识回答，也必须先在回答里明确说明"
        "「本轮上传资料中没有检索到可确认的依据」，再区分通用知识。"
    ),
    "DEGRADED": (
        "本轮知识检索不可用（Embedding / 向量检索失败），"
        "系统无法判断上传文档是否包含答案。"
        "不要声称资料中没有答案，也不要编造引用；回答时说明本次无法检索文档，"
        "通用知识必须明确区分。"
    ),
}

# 一段被裁到小于这个 Token 数就没有意义了，不如整段丢掉
MIN_USEFUL_SECTION_TOKENS = 40

_STATUS_LABEL = {
    UnderstandingStatus.UNKNOWN: "未接触",
    UnderstandingStatus.EXPOSED: "接触过",
    UnderstandingStatus.PARTIAL: "部分理解",
    UnderstandingStatus.DEMONSTRATED: "已能讲清",
    UnderstandingStatus.MISCONCEPTION: "存在误区",
}

_MEMORY_LABEL = {
    MemoryType.USER: "长期事实",
    # 学习类记忆是过去写下的状态判断，最容易与当前 Understanding 冲突，
    # 所以在标签上就标明历史属性，而不是只靠区块说明。
    MemoryType.LEARNING: "学习状态（历史记录）",
    MemoryType.PREFERENCE: "偏好",
}

# 记忆的作用域，渲染进 prompt 是为了让模型知道这条事实的适用范围：
# 标记成「全局」的是跨知识点都成立的长期偏好，不该被当成只是当前话题的细节。
_MEMORY_SCOPE_LABEL = {
    MemoryScopeType.GLOBAL: "全局",
    MemoryScopeType.SPACE: "本空间",
    MemoryScopeType.CONCEPT: "本知识点",
}

_CORRECTNESS_LABEL = {
    EvidenceCorrectness.CORRECT: "答对",
    EvidenceCorrectness.INCORRECT: "答错",
    EvidenceCorrectness.PARTIAL: "部分正确",
    EvidenceCorrectness.UNKNOWN: "未判定",
}

_EVIDENCE_TYPE_LABEL = {
    EvidenceType.USER_EXPLANATION: "用户解释",
    EvidenceType.OBSERVATION: "观察",
    EvidenceType.TEST_RESULT: "测试结果",
    EvidenceType.USER_CORRECTION: "用户纠正批注",
    EvidenceType.HIGHLIGHT_SIGNAL: "批注信号",
}

_SOURCE_TYPE_LABEL = {
    "MESSAGE": "消息",
    "HIGHLIGHT": "批注",
    "TEST_ANSWER": "测试回答",
}


@dataclass(slots=True)
class ContextSection:
    """上下文的一段。保留来源与预算信息，是为了让裁剪可解释。"""

    name: str
    title: str
    content: str
    priority: int
    source_ids: list[str] = field(default_factory=list)
    dedupe_key: str | None = None
    # 只有历史消息用得上：它最终会变成一条 role=user / assistant 的对话消息
    role: str | None = None
    allow_truncate: bool = False
    estimated_tokens: int = 0
    truncated: bool = False
    dropped: bool = False

    def __post_init__(self) -> None:
        self.estimated_tokens = estimate_tokens(self.content)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "priority": self.priority,
            "sourceIds": self.source_ids,
            "estimatedTokens": self.estimated_tokens,
            "truncated": self.truncated,
            "dropped": self.dropped,
            "preview": self.content[:200],
        }


@dataclass(slots=True)
class BuiltContext:
    system_instruction: str
    messages: list[ChatMessage]
    sections: list[ContextSection]
    estimated_tokens: int
    warnings: list[str] = field(default_factory=list)
    # 预算口径（Phase 1.1 明确）：
    # - `max_context_tokens` 约束的是**动态上下文**：区块标题/说明、section 内容、
    #   历史消息与实际用户输入；固定的 BASE_SYSTEM_INSTRUCTION（教学契约）不计入。
    # - `estimated_tokens` 是完整 prompt（含固定教学契约）的真实估算。
    # - `context_tokens` 是动态上下文部分，也就是真正被预算约束的值。
    # 这样 300 的测试预算仍可约束动态内容，同时报告不会把完整 prompt 伪装成 300。
    budget_tokens: int = 0
    context_tokens: int = 0
    budget_exceeded: bool = False
    budget_overrun_reason: str | None = None
    # 检索前的原始状态，以及预算裁剪后模型真正看到的最终状态。
    # 两者分开保存，避免把「检索成功」误报成「知识进入上下文」。
    knowledge_status: str = "NOT_RETRIEVED"
    knowledge_state: str = "NOT_RETRIEVED"
    knowledge_injected_chunk_ids: list[int] = field(default_factory=list)
    knowledge_dropped_chunk_ids: list[int] = field(default_factory=list)

    @property
    def dropped_sections(self) -> list[str]:
        return [section.name for section in self.sections if section.dropped]

    @property
    def user_message(self) -> str:
        return self.messages[-1].content if self.messages else ""

    def as_dict(self) -> dict[str, object]:
        """给 `/agent/context/preview` 用：完整上下文，供人核对检索与裁剪是否正确。"""
        return {
            "estimatedTokens": self.estimated_tokens,
            "contextTokens": self.context_tokens,
            "budgetTokens": self.budget_tokens,
            "budgetExceeded": self.budget_exceeded,
            "budgetOverrunReason": self.budget_overrun_reason,
            "knowledgeStatus": self.knowledge_status,
            "knowledgeState": self.knowledge_state,
            "knowledgeInjectedChunkIds": self.knowledge_injected_chunk_ids,
            "knowledgeDroppedChunkIds": self.knowledge_dropped_chunk_ids,
            "droppedSections": self.dropped_sections,
            "warnings": self.warnings,
            "sections": [section.as_dict() for section in self.sections],
            "systemInstruction": self.system_instruction,
            "messages": [message.as_dict() for message in self.messages],
        }


class ContextBuilder:
    def __init__(self, *, max_context_tokens: int = 3000, recent_history_limit: int = 12) -> None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须为正数")
        if recent_history_limit < 0:
            # 契约：0 = 不注入历史，负数非法。不要用 [-0:] 这种切片把 0 解释成全部。
            raise ValueError("recent_history_limit 不能为负数（0 表示不注入历史）")
        self._max_context_tokens = max_context_tokens
        self._recent_history_limit = recent_history_limit

    # ---------- 主流程 ----------

    def build(self, state: AgentState) -> BuiltContext:
        sections = self._dedupe(self._collect_sections(state))
        self._fit_budget(sections)

        system_instruction = self._render_system_instruction(sections)
        messages = self._render_messages(sections, system_instruction)

        # `_fit_budget` 只按 section 内容做一次快速裁剪；这里再按最终渲染结果
        # （标题、说明、实际消息，不含固定教学契约）收紧，直到动态上下文装得下。
        for _ in range(len(sections) + 2):
            context_tokens = _context_tokens(system_instruction, messages)
            if context_tokens <= self._max_context_tokens:
                break
            if not self._reduce_one(sections):
                break
            system_instruction = self._render_system_instruction(sections)
            messages = self._render_messages(sections, system_instruction)

        # 预算裁剪发生在检索之后。这里必须重新看「模型最终看到了哪些知识」，
        # 不能把检索层的 SUCCESS 直接当作知识已进入上下文。
        knowledge_state, injected_ids, dropped_ids = _final_knowledge_state(
            state, sections
        )
        if knowledge_state in {
            KNOWLEDGE_STATE_SUCCESS_PARTIAL,
            KNOWLEDGE_STATE_SUCCESS_BUT_DROPPED,
        }:
            sections = _upsert_budget_knowledge_status(
                sections,
                raw_status=state.knowledge_status,
                final_state=knowledge_state,
                injected=len(injected_ids),
                dropped=len(dropped_ids),
            )
            # 状态区块是预算裁剪后补上的；它的篇幅很小，但必须真的渲染给模型，
            # 否则「检索到但没放下」与「没有检索到」在 Prompt 里仍然无法区分。
            system_instruction = self._render_system_instruction(sections)
            messages = self._render_messages(sections, system_instruction)

        estimated = _prompt_tokens(messages)
        context_tokens = _context_tokens(system_instruction, messages)
        overrun_reason = None
        if context_tokens > self._max_context_tokens:
            overrun_reason = (
                f"动态上下文约 {context_tokens} tokens，超过预算 "
                f"{self._max_context_tokens}；完整 prompt 约 {estimated} tokens。"
                "已无低优先级区块可继续裁剪，未伪造估算。"
            )
        warnings = self._collect_warnings(state, sections)
        if knowledge_state == KNOWLEDGE_STATE_SUCCESS_BUT_DROPPED:
            warnings.append(
                "知识检索成功，但全部知识片段因 Token 预算未进入最终上下文；"
                "已在 system instruction 中明确说明，不能改写成「没有检索到资料」。"
            )
        elif knowledge_state == KNOWLEDGE_STATE_SUCCESS_PARTIAL:
            warnings.append(
                f"知识检索成功，但只有 {len(injected_ids)} 个片段进入最终上下文，"
                f"{len(dropped_ids)} 个因 Token 预算被丢弃；"
                "已在 system instruction 中说明只能引用实际出现的片段。"
            )
        if estimated > self._max_context_tokens:
            fixed = estimate_tokens(BASE_SYSTEM_INSTRUCTION)
            warnings.append(
                f"完整 prompt 约 {estimated} tokens，其中固定系统提示约 {fixed} tokens；"
                f"max_context_tokens={self._max_context_tokens} 约束的是动态上下文"
                f"（当前 {context_tokens} tokens），不是完整 prompt。"
            )
        if overrun_reason:
            warnings.append(overrun_reason)
        return BuiltContext(
            system_instruction=system_instruction,
            messages=messages,
            sections=sections,
            estimated_tokens=estimated,
            warnings=warnings,
            budget_tokens=self._max_context_tokens,
            context_tokens=context_tokens,
            budget_exceeded=overrun_reason is not None,
            budget_overrun_reason=overrun_reason,
            knowledge_status=state.knowledge_status,
            knowledge_state=knowledge_state,
            knowledge_injected_chunk_ids=injected_ids,
            knowledge_dropped_chunk_ids=dropped_ids,
        )

    # ---------- 1. 收集 ----------

    def _collect_sections(self, state: AgentState) -> list[ContextSection]:
        sections: list[ContextSection] = []

        # 本轮输入：最高优先级，且永不被丢（模型必须看到问题本身）
        sections.append(
            ContextSection(
                name="user_input",
                title="本轮提问",
                content=state.user_message,
                priority=PRIORITY_USER_INPUT,
                source_ids=[f"message:{state.user_message[:30]}"],
            )
        )

        # 对话历史：越近的优先级越高，预算不够时自然先丢最早的几轮
        history = self._usable_history(state)
        for distance, message in enumerate(reversed(history)):
            sections.append(
                ContextSection(
                    name=f"history:{len(history) - distance}",
                    title="历史消息",
                    content=message.content,
                    priority=PRIORITY_HISTORY - distance,
                    role="user" if message.is_user() else "assistant",
                )
            )

        # 本阶段学习目标：短期的「我们在做什么」，与下面 understanding 的「你学到哪了」是两回事。
        # session 为 None 是常态（懒创建，见 AgentState.session 的说明），跳过即可 ——
        # 不要为它编一个「未设置」的占位区块，那会让模型以为自己知道什么。
        if state.session is not None:
            rendered = _render_session(state.session)
            if rendered:
                sections.append(
                    ContextSection(
                        name="session",
                        title="本阶段学习目标",
                        content=rendered,
                        priority=PRIORITY_SESSION,
                        source_ids=[f"session:{state.session.id}"],
                        dedupe_key="session",
                    )
                )

        # 认知背景
        learning_memory_concept_ids = {
            memory.concept_id
            for memory in state.memories
            if memory.memory_type is MemoryType.LEARNING
        }
        for understanding in state.understandings:
            if not understanding.has_content:
                # 只有 UNKNOWN 空快照的概念不提供任何信息，塞进去只会浪费预算
                continue
            has_learning_memory = (
                None in learning_memory_concept_ids
                or understanding.concept_id in learning_memory_concept_ids
            )
            sections.append(
                ContextSection(
                    name=f"understanding:{understanding.concept_id}",
                    title=understanding.concept_name,
                    content=_render_understanding(
                        understanding, has_learning_memory=has_learning_memory
                    ),
                    priority=PRIORITY_UNDERSTANDING,
                    source_ids=[f"understanding:{understanding.id}"],
                    dedupe_key=f"understanding:{understanding.concept_id}",
                    allow_truncate=True,
                )
            )

        # 历史轨迹只在显式复盘时存在；普通问题 state.trajectories 为空，不占预算。
        for trajectory in state.trajectories:
            sections.append(
                ContextSection(
                    name=f"trajectory:{trajectory.concept_id}",
                    title=f"{trajectory.concept_name} 学习轨迹",
                    content=_render_trajectory(trajectory),
                    priority=PRIORITY_TRAJECTORY,
                    source_ids=[
                        *(f"revision:{item.id}" for item in trajectory.revisions),
                        *(f"evidence:{item.id}" for item in trajectory.evidences),
                    ],
                    dedupe_key=f"trajectory:{trajectory.concept_id}",
                    allow_truncate=True,
                )
            )

        trajectory_evidence_ids = {
            evidence.id
            for trajectory in state.trajectories
            for evidence in trajectory.evidences
        }

        for memory in state.memories:
            sections.append(
                ContextSection(
                    name=f"memory:{memory.id}",
                    title="长期记忆",
                    content=_render_memory(memory),
                    priority=PRIORITY_MEMORY,
                    source_ids=[f"memory:{memory.id}"],
                    dedupe_key=f"memory:{memory.id}",
                )
            )

        status_note = _render_knowledge_status(
            state.knowledge_status, state.knowledge_warning
        )
        if status_note:
            sections.append(
                ContextSection(
                    name="knowledge_status",
                    title="知识检索状态",
                    content=status_note,
                    priority=PRIORITY_KNOWLEDGE_STATUS,
                    source_ids=[f"knowledge-status:{state.knowledge_status}"],
                    dedupe_key="knowledge_status",
                )
            )

        for chunk in state.knowledge:
            sections.append(
                ContextSection(
                    name=f"knowledge:{chunk.id}",
                    title="参考资料",
                    content=_render_knowledge(chunk),
                    priority=PRIORITY_KNOWLEDGE,
                    source_ids=[f"chunk:{chunk.id}"],
                    dedupe_key=f"chunk:{chunk.id}",
                )
            )

        for evidence in state.evidences:
            # 复盘轨迹已经把关联证据按时间渲染过；再走通用 evidence 区块会重复注入。
            if evidence.id in trajectory_evidence_ids:
                continue
            if evidence.is_cross_concept:
                # 受控一跳扩展来的证据单独成块：它是其他概念的历史证据，
                # 只能作为参考，不能用来改写当前概念的 Understanding。
                sections.append(
                    ContextSection(
                        name=f"related_evidence:{evidence.id}",
                        title="相关概念判定依据（历史参考）",
                        content=_render_related_evidence(evidence),
                        priority=PRIORITY_RELATED_EVIDENCE,
                        source_ids=[f"evidence:{evidence.id}"],
                        dedupe_key=f"related_evidence:{evidence.id}",
                    )
                )
                continue
            sections.append(
                ContextSection(
                    name=f"evidence:{evidence.id}",
                    title="判定依据",
                    content=_render_evidence(evidence),
                    priority=PRIORITY_EVIDENCE,
                    source_ids=[f"evidence:{evidence.id}"],
                    dedupe_key=f"evidence:{evidence.id}",
                )
            )

        return sections

    def _usable_history(self, state: AgentState) -> list:
        if self._recent_history_limit <= 0:
            # 契约：0 = 不注入任何历史。`[-0:]` 会等于 `[0:]`，把 0 解释成全部，
            # 这是 Python 切片的一个安静陷阱。
            return []
        history = list(state.history)[-self._recent_history_limit :]
        # 防御性去重：后端已经保证 history 不含本轮输入（ChatService 用 userMessage.getId()
        # 把它排掉了），所以这段代码在正常情况下不会生效。
        #
        # 留着它是为了防「后端哪天把这个排重去掉」——那会让同一句话在 prompt 里出现两遍，
        # 模型容易把它理解成用户重复强调，进而答得比问的更用力。两害相权，宁可留这段。
        #
        # 代价要说清楚：它按**文本相等**判断，所以用户真的把同一个问题连问两遍时，
        # 第一遍会被当成「本轮输入的副本」丢掉 —— 这是它唯一的误判方向，
        # 损失是上下文里少一轮对话，不会答错。
        if history and history[-1].is_user() and history[-1].content.strip() == state.user_message.strip():
            history = history[:-1]
        return history

    # ---------- 2. 去重 ----------

    @staticmethod
    def _dedupe(sections: list[ContextSection]) -> list[ContextSection]:
        seen: dict[str, ContextSection] = {}
        result: list[ContextSection] = []
        for section in sorted(sections, key=lambda item: item.priority, reverse=True):
            key = section.dedupe_key
            if key is None:
                result.append(section)
                continue
            if key in seen:
                continue
            seen[key] = section
            result.append(section)
        return result

    # ---------- 3. 控 Token ----------

    def _fit_budget(self, sections: list[ContextSection]) -> None:
        """按优先级从高到低装，装不下的丢。

        只有 `allow_truncate` 的段落会被裁一半留下 —— 认知背景被裁掉尾部仍然有用；
        而历史消息、参考资料、证据是原子的，半条消息比没有更误导。
        """
        remaining = self._max_context_tokens
        for section in sorted(sections, key=lambda item: item.priority, reverse=True):
            if section.estimated_tokens <= remaining:
                remaining -= section.estimated_tokens
                continue
            if section.allow_truncate and remaining >= MIN_USEFUL_SECTION_TOKENS:
                section.content = truncate_to_tokens(section.content, remaining)
                section.truncated = True
                section.estimated_tokens = estimate_tokens(section.content)
                remaining -= section.estimated_tokens
                continue
            # 本轮输入即使超预算也保留：模型看不到问题，这次回答就没有意义
            if section.name == "user_input":
                section.content = truncate_to_tokens(section.content, max(remaining, 1))
                section.truncated = True
                section.estimated_tokens = estimate_tokens(section.content)
                remaining = 0
                continue
            section.dropped = True

    def _reduce_one(self, sections: list[ContextSection]) -> bool:
        """按优先级从低到高再收紧一段；返回 False 表示已无可删内容。

        优先截断允许截断的区块，其次整段丢弃。`user_input` 永不丢弃 ——
        模型看不到问题，这次回答就没有意义。
        """
        candidates = [
            section
            for section in sections
            if not section.dropped
            and section.name not in {"user_input", "session"}
        ]
        if not candidates:
            return False
        section = min(candidates, key=lambda item: item.priority)
        if section.allow_truncate and section.estimated_tokens > MIN_USEFUL_SECTION_TOKENS:
            before = section.estimated_tokens
            target = max(MIN_USEFUL_SECTION_TOKENS, before // 2)
            section.content = truncate_to_tokens(section.content, target)
            section.estimated_tokens = estimate_tokens(section.content)
            section.truncated = True
            if section.estimated_tokens < before:
                return True
        section.dropped = True
        return True

    # ---------- 4/5. 渲染 ----------

    def _render_system_instruction(self, sections: list[ContextSection]) -> str:
        blocks: list[str] = [BASE_SYSTEM_INSTRUCTION]

        for name in RENDER_ORDER:
            group = [section for section in sections if _kind(section) == name and not section.dropped]
            if not group:
                continue
            heading, note = _BLOCK_HEADINGS[name]
            body = "\n".join(section.content for section in group)
            blocks.append(f"## {heading}\n{note}\n\n{body}")

        # 这句话只描述「历史认知记录」。知识状态 / 知识片段 / 判定依据都不属于
        # 学习者认知记录，不能让它们把「没有历史认知」这句话顶掉。
        cognitive_kinds = {"session", "understanding", "trajectory", "memory"}
        kept_cognitive = any(
            _kind(section) in cognitive_kinds and not section.dropped
            for section in sections
        )
        dropped_records = [
            section
            for section in sections
            if _kind(section) in {"understanding", "trajectory", "memory"}
            and section.dropped
        ]
        kept_records = any(
            _kind(section) in {"understanding", "trajectory", "memory"}
            and not section.dropped
            for section in sections
        )
        if not kept_cognitive and not dropped_records:
            # 真的什么都没检索到，才是「没有历史认知记录」。
            blocks.append("## 学习者背景\n（本次没有检索到任何历史认知记录，按新知识正常教学。）")
        elif dropped_records and not kept_records:
            # 检索到了，只是被预算丢掉：不能对用户谎称「没有历史」。
            names = "、".join(section.name for section in dropped_records)
            blocks.append(
                "## 学习者背景\n"
                f"（本次检索到了历史认知记录，但以下区块超出上下文预算、未包含在本次 prompt 中：{names}。"
                "回答时不要说「没有历史记录」或「没有检索到记录」；需要时说明本次上下文未包含这些记录。）"
            )
        return "\n\n".join(blocks)

    def _render_messages(
        self, sections: list[ContextSection], system_instruction: str
    ) -> list[ChatMessage]:
        history = sorted(
            (section for section in sections if _kind(section) == "history" and not section.dropped),
            key=lambda section: int(section.name.split(":")[1]),
        )
        user_input = next(section for section in sections if section.name == "user_input")

        messages = [ChatMessage(role="system", content=system_instruction)]
        messages.extend(
            ChatMessage(role=section.role or "user", content=section.content) for section in history
        )
        messages.append(ChatMessage(role="user", content=user_input.content))
        return messages

    @staticmethod
    def _collect_warnings(state: AgentState, sections: list[ContextSection]) -> list[str]:
        warnings: list[str] = []
        # UNKNOWN 且什么都不含的快照不算「检索到了」—— 它提供不了任何可用信息，
        # 所以这里看的是可用的快照，而不是列表是否为空。
        if not any(understanding.has_content for understanding in state.understandings):
            warnings.append("没有检索到任何 Understanding：这次回答无法接着用户已有认知讲。")
        # 知识检索的正常空结果 / 阈值过滤 / 依赖失败由检索层给出明确原因。
        # 这里只负责把它带进 context preview 与 meta，不把失败伪装成「没有资料」。
        if state.knowledge_warning:
            warnings.append(state.knowledge_warning)
        # 相关概念查询失败必须与「没有相关概念」区分开：前者是依赖失败，
        # 后者是正常的空结果。这里只把检索层给出的失败原因带出来。
        if state.related_concepts_warning:
            warnings.append(state.related_concepts_warning)
        pending = [section.name for section in sections if section.dropped]
        if pending:
            warnings.append(f"以下片段因超出 Token 预算被丢弃：{', '.join(pending)}")
        truncated = [section.name for section in sections if section.truncated]
        if truncated:
            warnings.append(f"以下片段被截断：{', '.join(truncated)}")
        return warnings


_BLOCK_HEADINGS = {
    "session": (
        "本阶段学习目标（当前这段学习活动）",
        "> 用户正在进行的这一段学习最终想解决什么、此刻卡在哪里。它是这一轮回答的框架与落点，"
        "不等于用户已经掌握的内容。回答要往这个目标上靠，但不要逐字复述它。",
    ),
    "understanding": (
        "学习者的当前认知（系统长期记录）",
        "> 这是系统对「这个用户现在怎么理解这些概念」的判断，用来决定**从哪讲起**。"
        "只有「已经理解」明确列出的内容才算已掌握；状态「已能讲清」不覆盖未列出的相关知识，"
        "「部分理解」「接触过」「未接触」都不能说成已掌握。"
        "不要向用户复述这一段的结构，也不要说「根据我的记录」。",
    ),
    "trajectory": (
        "学习轨迹（历史记录，不等于当前状态）",
        "> 这些是用户过去真实发生过的版本与证据，只在复盘时使用。"
        "已纠正的历史误区必须说成「过去曾经」，不能描述成当前仍在的误区；"
        "只能陈述这里的原话、批注、测试回答、状态与时间，不得补写「意识到」「推导出」"
        "「想通」等未被记录的心理过程；记录没写原因时就说看不出原因。"
        "尤其不要断言纠正是否借助外部资料、是否由用户独立完成。"
        "当前状态以「学习者的当前认知」区块为准，历史不能覆盖它。",
    ),
    "memory": (
        "这个用户的长期记忆（历史记录）",
        "> 用来调整讲解方式。这些是过去写下的历史记录，不等于用户此刻的认知；"
        "与当前认知/本轮证据冲突时，说明这是较早的记录、当前状态已经不同，"
        "但不要推断用户为什么改变，也不要删除或改写长期记忆，"
        "也不要把旧记忆无条件当成本轮事实。与当前问题无关的记忆不要主动展开。",
    ),
    "knowledge_status": (
        "本轮知识检索状态",
        "> 这决定了本轮能不能把上传资料当作依据。按状态说明回答，不要自行补成「资料不存在」。",
    ),
    "knowledge": (
        "参考资料（来自用户上传的文档）",
        "> 这些是检索到的文档片段，不是绝对真理。只能引用这里实际出现的来源标注，"
        "不要编造文件名或片段序号；资料不足时明确说明。不同资料/版本的事实要拆句，"
        "并分别标注各自来源；不要把两个来源的数字合并成一句后只标一个来源。"
        "与学习者认知冲突时，先确认是不是误区，再决定怎么讲。",
    ),
    "evidence": (
        "判定依据",
        "> 只在这些依据被问起（「为什么你认为我理解了」）或需要复盘时使用，不要主动罗列。",
    ),
    "related_evidence": (
        "相关概念判定依据（历史参考）",
        "> 这些证据来自与本轮概念存在 PREREQUISITE / RELATED 关系的其他概念，"
        "通过受控一跳扩展得到，仅作历史参考。它们属于各自标注的概念，"
        "不能改写、推断或覆盖「学习者的当前认知」中当前概念的 Understanding；"
        "不要把它们说成当前概念自己的掌握证据，也不要主动罗列。",
    ),
}


def _kind(section: ContextSection) -> str:
    """`understanding:10` → `understanding`。历史消息是 `history:3`。"""
    return section.name.split(":", 1)[0]


def _prompt_tokens(messages: list[ChatMessage]) -> int:
    """完整 prompt 的估算：system instruction + 所有实际消息。"""
    return sum(estimate_tokens(message.content) for message in messages)


def _context_tokens(system_instruction: str, messages: list[ChatMessage]) -> int:
    """动态上下文：完整 prompt 减去固定教学契约。

    固定契约（BASE_SYSTEM_INSTRUCTION）不是可裁剪内容，也不算进
    `max_context_tokens`；但区块标题/说明、section 内容和实际消息都要算。
    """
    fixed = estimate_tokens(BASE_SYSTEM_INSTRUCTION)
    return max(0, _prompt_tokens(messages) - fixed)


def _render_session(session: SessionContext) -> str:
    """渲染本阶段学习目标。两个字段都没有时返回空串，让调用方整块跳过。"""
    lines: list[str] = []
    if session.goal:
        lines.append(f"- 这一阶段要解决：{session.goal}")
    if session.current_focus:
        lines.append(f"- 当前焦点：{session.current_focus}")
    return "\n".join(lines)


def _render_understanding(
    understanding: Understanding, *, has_learning_memory: bool = False
) -> str:
    label = _STATUS_LABEL.get(understanding.status, understanding.status.value)
    lines = [f"### {understanding.concept_name} ｜ 状态：{label}"]
    if understanding.summary:
        lines.append(f"- 当前理解：{understanding.summary}")
    if understanding.known_parts:
        lines.append(
            "- 已经理解（只有这个列表里的内容可以称为已掌握）："
            + "；".join(understanding.known_parts)
        )
        lines.append(
            "- 这个列表之外的相关知识，即使本轮讲解提到，也不能说成用户已经掌握。"
        )
    if understanding.misconceptions:
        lines.append("- 仍然存在的误区（要针对性纠正，指出推理在哪一步分叉，而不是直接说「你错了」）：")
        for item in understanding.misconceptions:
            marker = "当前存在" if item.status is MisconceptionStatus.ACTIVE else "已有反向证据，可能正在解除"
            lines.append(f"  - [{marker}] {item.text}")
    if understanding.uncertain_parts:
        lines.append("- 还不确定（本次可以重点补）：" + "；".join(understanding.uncertain_parts))
    if understanding.latest_user_explanation:
        lines.append(f"- 用户自己的说法：「{understanding.latest_user_explanation}」")
    if has_learning_memory:
        lines.append(
            "- 注意：长期记忆里另有一条较早的学习状态记录，可能与本条当前认知不同；"
            "回答直接相关问题时，说明它是历史记录、当前状态已不同，不要把它当成本轮事实。"
        )
    # 这条规则在 Trajectory-enabled 与 Current-only 两种上下文里都必须存在，
    # 否则两种模式的非轨迹输入就不一致，C 的对照会失去意义。
    lines.append(
        "- 如果本轮没有提供历史轨迹：不要从当前状态反推过去，也不要把"
        "「没有看到错误记录」说成「过去没有错误」；用户问历史时说明当前可见信息不足。"
    )
    return "\n".join(lines)


def _render_memory(memory: MemoryItem) -> str:
    label = _MEMORY_LABEL.get(memory.memory_type, memory.memory_type.value)
    scope = _MEMORY_SCOPE_LABEL.get(memory.scope_type, memory.scope_type.value)
    times: list[str] = []
    if memory.created_at:
        times.append(f"记录于 {_format_time(memory.created_at)}")
    if memory.updated_at and memory.updated_at != memory.created_at:
        times.append(f"更新于 {_format_time(memory.updated_at)}")
    suffix = f"（{'；'.join(times)}）" if times else ""
    return f"- [{label}，作用域：{scope}] {memory.content}{suffix}"


def _render_trajectory(trajectory: LearningTrajectory) -> str:
    """渲染「过去发生的事实」，与当前理解严格分开。

    这里只使用 Revision / Evidence 原始字段，不生成任何模型总结：
    版本号、时间、状态、误区原文、来源类型都是持久化数据。内部 id 不进入
    面向用户的文本，只保留在 `ContextSection.source_ids` 与评测产物里。
    """
    label = _STATUS_LABEL.get(
        trajectory.current_status, trajectory.current_status.value
    )
    lines = [
        f"### {trajectory.concept_name} ｜ 学习轨迹（历史）",
        f"- 当前状态（来自持久化快照）：{label}",
    ]

    if trajectory.current_misconceptions:
        lines.append(
            "- 当前仍存在的误区：" + "；".join(trajectory.current_misconceptions)
        )

    historical = historical_misconceptions(trajectory)
    for text in historical:
        first = misconception_first_seen(trajectory, text)
        last_status = misconception_last_status(trajectory, text)
        first_time = f"，首次记录于 {_format_time(first.created_at)}" if first and first.created_at else ""
        # 当前快照已没有这条误区，所以无论历史最后是 ACTIVE 还是 PENDING，
        # 对今天而言都是「已纠正的历史」；PENDING 只说明当时证据已开始反转。
        detail = "，当时已有反向证据但尚未解除" if last_status is MisconceptionStatus.PENDING else ""
        lines.append(f"- [已纠正，历史] {text}{first_time}{detail}")

    if trajectory.revisions:
        lines.append("- 关键版本（按时间）：")
        for revision in trajectory.revisions:
            parts = [
                f"版本 {revision.revision_no}",
                _format_time(revision.created_at),
                f"状态：{_STATUS_LABEL.get(revision.status, revision.status.value)}",
            ]
            if revision.misconceptions:
                mistakes = "；".join(
                    f"「{item.text}」[当时 {'ACTIVE' if item.status is MisconceptionStatus.ACTIVE else 'PENDING（已有反向证据）'}]"
                    for item in revision.misconceptions
                )
                parts.append(f"误区：{mistakes}")
            lines.append("  - " + " ｜ ".join(part for part in parts if part))

    correction_evidence = [
        evidence
        for evidence in trajectory.evidences
        if evidence.correctness is EvidenceCorrectness.CORRECT
    ]
    if correction_evidence:
        times = "、".join(
            _format_time(evidence.created_at) or "时间未记录"
            for evidence in correction_evidence
        )
        lines.append(f"- 后续正确证据（{len(correction_evidence)} 条）：{times}")

    if trajectory.evidences:
        lines.append("- 关联证据（按时间）：")
        for evidence in trajectory.evidences:
            lines.append("  - " + _render_trajectory_evidence(evidence))

    lines.append(
        "- 以上是全部可核对记录；未记录的动机、原因、是否借助外部资料，"
        "均无法确认，不要补写。"
    )
    return "\n".join(lines)


def _render_trajectory_evidence(evidence: EvidenceItem) -> str:
    correctness = _CORRECTNESS_LABEL.get(
        evidence.correctness, evidence.correctness.value
    )
    evidence_type = _EVIDENCE_TYPE_LABEL.get(
        evidence.evidence_type, evidence.evidence_type.value
    )
    # 面向用户的历史来源只用可读的类型与时间，不渲染数据库证据 / 来源 id。
    # 评测需要的精确 id 仍在 ContextSection.source_ids 与评测产物的 trajectoryFacts 里。
    sources = "、".join(
        _SOURCE_TYPE_LABEL.get(source.source_type, source.source_type)
        for source in evidence.sources
    )
    parts = [
        _format_time(evidence.created_at),
        f"[{correctness}/{evidence_type}]",
        f"来源：{sources}" if sources else "",
        evidence.observation,
    ]
    return " ｜ ".join(part for part in parts if part)


def _format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _final_knowledge_state(
    state: AgentState, sections: list[ContextSection]
) -> tuple[str, list[int], list[int]]:
    """把「检索状态」和「预算后的实际注入」合成一个最终状态。

    返回 `(state, injected_chunk_ids, dropped_chunk_ids)`。只有检索层状态为
    SUCCESS 时才可能被改写成 PARTIAL / BUT_DROPPED；其他状态保持检索层原值，
    它们本来就表示「没有知识片段」，预算不可能把不存在的片段再丢掉。
    """
    raw = str(state.knowledge_status or "NOT_RETRIEVED")
    knowledge_sections = [
        section for section in sections if _kind(section) == "knowledge"
    ]
    kept = [section for section in knowledge_sections if not section.dropped]
    dropped = [section for section in knowledge_sections if section.dropped]
    injected_ids = [_chunk_id(section) for section in kept]
    dropped_ids = [_chunk_id(section) for section in dropped]

    if raw != "SUCCESS":
        return raw, injected_ids, dropped_ids
    if not knowledge_sections:
        return KNOWLEDGE_STATE_SUCCESS, injected_ids, dropped_ids
    if not kept:
        return KNOWLEDGE_STATE_SUCCESS_BUT_DROPPED, injected_ids, dropped_ids
    if dropped:
        return KNOWLEDGE_STATE_SUCCESS_PARTIAL, injected_ids, dropped_ids
    return KNOWLEDGE_STATE_SUCCESS, injected_ids, dropped_ids


def _chunk_id(section: ContextSection) -> int:
    try:
        return int(section.name.split(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _upsert_budget_knowledge_status(
    sections: list[ContextSection],
    *,
    raw_status: str,
    final_state: str,
    injected: int,
    dropped: int,
) -> list[ContextSection]:
    """在预算裁剪后补上（或替换）知识状态区块。

    该区块不参与前面的预算分配：它正是用来解释「为什么没有知识片段」的，
    如果也因为没有预算而被丢掉，模型就永远看不到真实原因。
    """
    content = _budget_knowledge_status_note(final_state, injected, dropped)
    for section in sections:
        if section.name == "knowledge_status":
            section.content = content
            section.estimated_tokens = estimate_tokens(content)
            section.dropped = False
            section.source_ids = [
                f"knowledge-status:{final_state}",
                f"knowledge-retrieval:{raw_status}",
            ]
            return sections
    return [
        *sections,
        ContextSection(
            name="knowledge_status",
            title="知识检索状态",
            content=content,
            priority=PRIORITY_KNOWLEDGE_STATUS,
            source_ids=[
                f"knowledge-status:{final_state}",
                f"knowledge-retrieval:{raw_status}",
            ],
            dedupe_key="knowledge_status",
        ),
    ]


def _budget_knowledge_status_note(
    final_state: str, injected: int, dropped: int
) -> str:
    total = injected + dropped
    if final_state == KNOWLEDGE_STATE_SUCCESS_BUT_DROPPED:
        return "\n".join(
            [
                "- 状态：SUCCESS_BUT_DROPPED",
                f"- 检索结果：检索成功，找到 {total} 个可用资料片段。",
                "- 预算结果：这些片段全部因上下文 Token 预算未能放入本次 Prompt。",
                "- 说明：这不等于「没有检索到资料」，也不等于「资料中没有答案」。"
                "不要声称本轮没有检索到资料；如果依据不足，请说明本轮上下文未能包含"
                "已检索到的资料，并明确区分通用知识；不要编造资料引用。",
            ]
        )
    return "\n".join(
        [
            "- 状态：SUCCESS_PARTIAL",
            f"- 检索结果：检索成功，找到 {total} 个资料片段。",
            f"- 预算结果：{injected} 个已放入本次 Prompt，{dropped} 个因 Token 预算未放入。",
            "- 说明：只能依据「参考资料」中实际出现的片段；不要声称本轮没有检索到资料，"
            "也不要把未放入的片段当作依据或编造引用。",
        ]
    )


def _render_knowledge_status(status: str, warning: str | None) -> str:
    note = _KNOWLEDGE_STATUS_NOTES.get(str(status or ""))
    if not note:
        return ""
    lines = [f"- 状态：{status}", f"- 说明：{note}"]
    if warning:
        lines.append(f"- 检索层记录：{warning}")
    return "\n".join(lines)


def _render_knowledge(chunk: KnowledgeChunk) -> str:
    """可核对的来源标注：优先文档名 + 片段序号，退化时才用内部片段 ID。

    模型只能引用这里实际出现过的标注；不要渲染数据库 chunk_id 当作来源名，
    那既不可读，也会让模型误以为任意数字都可以被引用。
    """
    label = format_source_label(chunk)

    # 章节/页码必须放在方括号**外面**：方括号内是要被模型原样复制的引用标注，
    # 只能是 `[资料《文件名》#序号]`。把章节塞进去会让模型复制出
    # `[资料《文件》#0 ｜ 章节：…]`，校验器按精确格式解析时判为 malformed。
    suffix = ""
    heading = " > ".join(chunk.heading_path).strip() if chunk.heading_path else ""
    if heading and not chunk.content.lstrip().startswith(heading):
        suffix += f" ｜ 章节：{heading}"
    if chunk.page_number:
        suffix += f" ｜ 第 {chunk.page_number} 页"
    return f"- [{label}]{suffix} {chunk.content}"


def _render_evidence(evidence) -> str:
    label = _CORRECTNESS_LABEL.get(evidence.correctness, evidence.correctness.value)
    evidence_type = _EVIDENCE_TYPE_LABEL.get(
        evidence.evidence_type, evidence.evidence_type.value
    )
    when = _format_time(evidence.created_at)
    prefix = f"{when} ｜ " if when else ""
    return f"- [{label}/{evidence_type}] {prefix}{evidence.observation}"


def _render_related_evidence(evidence) -> str:
    """渲染跨概念证据：明确标注扩展依据、发起概念与真实归属。

    `related_via` / `source_concept_id` / `is_cross_concept` 是 Agent 侧标记，
    `concept_id` 始终是这条证据的真实归属，不能被改写。
    """
    label = _CORRECTNESS_LABEL.get(evidence.correctness, evidence.correctness.value)
    evidence_type = _EVIDENCE_TYPE_LABEL.get(
        evidence.evidence_type, evidence.evidence_type.value
    )
    when = _format_time(evidence.created_at)
    prefix = f"{when} ｜ " if when else ""
    via = evidence.related_via or "RELATED"
    source = (
        f"（经概念 #{evidence.source_concept_id} 关联）"
        if evidence.source_concept_id is not None
        else ""
    )
    return (
        f"- [{label}/{evidence_type}] 关联依据：{via}{source}"
        f" ｜ 证据归属概念：#{evidence.concept_id} ｜ {prefix}{evidence.observation}"
    )
