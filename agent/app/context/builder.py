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

from app.citations import format_source_label
from app.context.tokens import estimate_tokens, truncate_to_tokens
from app.llm.base import ChatMessage
from app.prompts import BASE_SYSTEM_INSTRUCTION
from app.schemas import (
    EvidenceCorrectness,
    KnowledgeChunk,
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
PRIORITY_MEMORY = 50
# 知识检索状态比知识片段本身略高：即使片段全部被预算丢掉，模型也必须知道
# 「这次是没有可用文档 / 阈值过滤 / 检索降级」，而不是把空知识区当成「资料里没有」。
PRIORITY_KNOWLEDGE_STATUS = 45
PRIORITY_KNOWLEDGE = 40
PRIORITY_EVIDENCE = 20

# 渲染进 System Instruction 时的区块顺序（与优先级无关：优先级决定谁被丢，
# 顺序决定人读起来顺不顺）。session 放最前：它是后面一切内容的框架。
RENDER_ORDER = (
    "session",
    "understanding",
    "memory",
    "knowledge_status",
    "knowledge",
    "evidence",
)

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
    MemoryType.LEARNING: "学习状态",
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
            "droppedSections": self.dropped_sections,
            "warnings": self.warnings,
            "sections": [section.as_dict() for section in self.sections],
            "systemInstruction": self.system_instruction,
            "messages": [message.as_dict() for message in self.messages],
        }


class ContextBuilder:
    def __init__(self, *, max_context_tokens: int = 3000, recent_history_limit: int = 12) -> None:
        self._max_context_tokens = max_context_tokens
        self._recent_history_limit = recent_history_limit

    # ---------- 主流程 ----------

    def build(self, state: AgentState) -> BuiltContext:
        sections = self._dedupe(self._collect_sections(state))
        self._fit_budget(sections)

        system_instruction = self._render_system_instruction(sections)
        messages = self._render_messages(sections, system_instruction)

        used = sum(estimate_tokens(message.content) for message in messages)
        return BuiltContext(
            system_instruction=system_instruction,
            messages=messages,
            sections=sections,
            estimated_tokens=used,
            warnings=self._collect_warnings(state, sections),
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
        for understanding in state.understandings:
            if not understanding.has_content:
                # 只有 UNKNOWN 空快照的概念不提供任何信息，塞进去只会浪费预算
                continue
            sections.append(
                ContextSection(
                    name=f"understanding:{understanding.concept_id}",
                    title=understanding.concept_name,
                    content=_render_understanding(understanding),
                    priority=PRIORITY_UNDERSTANDING,
                    source_ids=[f"understanding:{understanding.id}"],
                    dedupe_key=f"understanding:{understanding.concept_id}",
                    allow_truncate=True,
                )
            )

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
        cognitive_kinds = {"session", "understanding", "memory"}
        if not any(
            _kind(section) in cognitive_kinds and not section.dropped
            for section in sections
        ):
            blocks.append("## 学习者背景\n（本次没有检索到任何历史认知记录，按新知识正常教学。）")
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
        "不要向用户复述这一段的结构，也不要说「根据我的记录」。",
    ),
    "memory": (
        "这个用户的长期背景与偏好",
        "> 用来调整讲解方式。当前用户本轮明确表达的意思，永远优先于这里的历史偏好。",
    ),
    "knowledge_status": (
        "本轮知识检索状态",
        "> 这决定了本轮能不能把上传资料当作依据。按状态说明回答，不要自行补成「资料不存在」。",
    ),
    "knowledge": (
        "参考资料（来自用户上传的文档）",
        "> 这些是检索到的文档片段，不是绝对真理。只能引用这里实际出现的来源标注，"
        "不要编造文件名或片段序号；资料不足时明确说明。与学习者认知冲突时，"
        "先确认是不是误区，再决定怎么讲。",
    ),
    "evidence": (
        "判定依据",
        "> 只在这些依据被问起（「为什么你认为我理解了」）或需要复盘时使用，不要主动罗列。",
    ),
}


def _kind(section: ContextSection) -> str:
    """`understanding:10` → `understanding`。历史消息是 `history:3`。"""
    return section.name.split(":", 1)[0]


def _render_session(session: SessionContext) -> str:
    """渲染本阶段学习目标。两个字段都没有时返回空串，让调用方整块跳过。"""
    lines: list[str] = []
    if session.goal:
        lines.append(f"- 这一阶段要解决：{session.goal}")
    if session.current_focus:
        lines.append(f"- 当前焦点：{session.current_focus}")
    return "\n".join(lines)


def _render_understanding(understanding: Understanding) -> str:
    label = _STATUS_LABEL.get(understanding.status, understanding.status.value)
    lines = [f"### {understanding.concept_name} ｜ 状态：{label}"]
    if understanding.summary:
        lines.append(f"- 当前理解：{understanding.summary}")
    if understanding.known_parts:
        lines.append("- 已经理解：" + "；".join(understanding.known_parts))
    if understanding.misconceptions:
        lines.append("- 仍然存在的误区（要针对性纠正，指出推理在哪一步分叉，而不是直接说「你错了」）：")
        for item in understanding.misconceptions:
            marker = "当前存在" if item.status is MisconceptionStatus.ACTIVE else "已有反向证据，可能正在解除"
            lines.append(f"  - [{marker}] {item.text}")
    if understanding.uncertain_parts:
        lines.append("- 还不确定（本次可以重点补）：" + "；".join(understanding.uncertain_parts))
    if understanding.latest_user_explanation:
        lines.append(f"- 用户自己的说法：「{understanding.latest_user_explanation}」")
    return "\n".join(lines)


def _render_memory(memory: MemoryItem) -> str:
    label = _MEMORY_LABEL.get(memory.memory_type, memory.memory_type.value)
    scope = _MEMORY_SCOPE_LABEL.get(memory.scope_type, memory.scope_type.value)
    return f"- [{label}，作用域：{scope}] {memory.content}"


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
    return f"- [{label}] {evidence.observation}"
