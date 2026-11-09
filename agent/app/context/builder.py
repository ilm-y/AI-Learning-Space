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

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.citations import format_source_label
from app.context.budget import (
    ELASTIC_KIND_ORDER,
    INJECTION_EXCLUDED,
    INJECTION_OMITTED,
    INJECTION_PARTIAL,
    INJECTION_SELECTED,
    INJECTION_TRUNCATED,
    MANIFEST_SCHEMA_VERSION,
    PROTECTED_KINDS,
    REASON_CHAR_TRUNCATED,
    REASON_DUPLICATE_SOURCE,
    REASON_GLOBAL_BUDGET,
    REASON_NOT_REVIEW_INTENT,
    REASON_POLICY_SKIP,
    REASON_SEMANTIC_REDUNDANCY,
    REASON_TIER_PREFIX,
    REASON_TYPE_BUDGET,
    REASON_WITHIN_BUDGET,
    TIER_FULL,
    TIER_ONE_LINE,
    TIER_STRUCTURED,
    BudgetPolicy,
)
from app.context.policy import Intent, RetrievalNeed, RetrievalPlan
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
# 学习认知 / 记忆 / 轨迹的检索状态比认知内容本身还高一点：它解释的是
# 「本轮有没有查、查成没查成」。降级说明很短，但决定了模型能不能把
# 「没看到记录」说成「用户没有记录」。预算紧张时内容可丢，这条不能丢。
PRIORITY_COGNITIVE_STATUS = 71
# 历史轨迹低于当前理解：复盘要能看到过去，但当前状态永远先被保留。
PRIORITY_TRAJECTORY = 68
PRIORITY_MEMORY = 50
# 知识检索状态比知识片段本身略高：即使片段全部被预算丢掉，模型也必须知道
# 「这次是没有可用文档 / 阈值过滤 / 检索降级」，而不是把空知识区当成「资料里没有」。
PRIORITY_KNOWLEDGE_STATUS = 45
PRIORITY_KNOWLEDGE = 40
# 概念解析状态比知识检索状态还高一点：它解释的是「本轮有没有理解用户指哪个概念」，
# 是后面 U / M / E 是否可信的前提。降级说明很短，不能被预算丢掉。
PRIORITY_CONCEPT_STATUS = 47
# 证据检索状态高于证据本身：降级说明很短，但它决定了模型能不能把「没看见证据」
# 说成「没有历史」。预算紧张时，证据条目可以被丢，这条解释不能被丢。
PRIORITY_EVIDENCE_STATUS = 46
PRIORITY_EVIDENCE = 20
# 跨概念扩展证据优先级更低：它是「相关概念的历史参考」，永远不能挤掉
# 当前概念自己的判定依据。
PRIORITY_RELATED_EVIDENCE = 18

# 渲染进 System Instruction 时的区块顺序（与优先级无关：优先级决定谁被丢，
# 顺序决定人读起来顺不顺）。session 放最前：它是后面一切内容的框架。
RENDER_ORDER = (
    "session",
    "cognitive_status",
    "understanding",
    "trajectory",
    "memory",
    "concept_status",
    "knowledge_status",
    "knowledge",
    "evidence_status",
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
    # ---------- D4-3：Manifest / 预算所需的元数据（不含正文）----------
    # `source_type` 与 section 前缀一致，单独存一份是为了让 Manifest 不依赖
    # section 命名格式；`retrieval_status` 保存检索层状态，避免把「没查」
    # 和「查了为空」在 Manifest 里混成一个值。
    source_type: str = ""
    retrieval_status: str = ""
    # protected 区块先占预算：Session 与状态说明被丢掉会让模型把「没看到」
    # 说成「没有」，它们不值得为任何弹性内容让路。
    protected: bool = False
    # 当前采用的压缩档位；full / structured / one_line。
    tier: str = TIER_FULL
    # 分层压缩时由收集阶段挂上：入参是档位，返回该档位的渲染文本。
    render_tier: Callable[[str], str] | None = None
    # 被排除（policy / duplicate）或超预算（type / global）的稳定原因。
    exclusion_reason: str | None = None
    # 被保留但降级（tier / 字符截断 / 语义冗余）的原因。
    compression_reason: str | None = None
    # 最终注入状态：SELECTED / TRUNCATED / OMITTED / EXCLUDED。
    injection_status: str = ""
    # 语义冗余保护的比较底稿：只含该条目的实质内容，不含区块标题/固定说明。
    # 守卫规则（否定词 / 数字 / 实体）必须看正文，不能看渲染骨架 ——
    # Understanding 的 full 档固定说明本身就含「没有」「不要」等词，
    # 拿渲染文本做否定词比较会把正常冗余全部误拦。
    guard_text: str = ""

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
    # 证据检索状态必须随上下文一起可观测：DEGRADED 不能只在日志里，
    # 否则调用方看到的仍是一次「正常但没有证据」的上下文。
    evidence_status: str = "NOT_RETRIEVED"
    evidence_warning: str | None = None
    cross_concept_evidence_status: str = "NOT_RETRIEVED"
    cross_concept_evidence_warning: str | None = None
    # E2：概念解析状态必须随上下文一起可观测。DEGRADED 不等于「用户没有历史」，
    # 也不能只在日志里，否则调用方看到的仍是一次「正常但没有认知记录」的组装。
    resolver_status: str = "NOT_CALLED"
    resolver_intent: str = "other"
    resolver_warning: str | None = None
    # 检索阶段的学习认知 / 记忆 / 轨迹状态。把它们随上下文一起返回，
    # 保证「没查到」「没查」「查失败」在 preview 与调用方眼里仍然可区分。
    understanding_status: str = "NOT_QUERIED"
    understanding_warning: str | None = None
    memory_status: str = "NOT_QUERIED"
    memory_warning: str | None = None
    memory_detail: str | None = None
    revision_status: str = "NOT_QUERIED"
    revision_warning: str | None = None
    # D4-3 最小 Manifest：只含 source_id / 状态 / 原因 / token 估算与预算汇总。
    # 刻意不包含正文、完整 prompt、query 或异常原文；不是可查询的持久化结构。
    context_manifest: dict[str, object] = field(default_factory=dict)

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
            "evidenceStatus": self.evidence_status,
            "evidenceWarning": self.evidence_warning,
            "crossConceptEvidenceStatus": self.cross_concept_evidence_status,
            "crossConceptEvidenceWarning": self.cross_concept_evidence_warning,
            "resolverStatus": self.resolver_status,
            "resolverIntent": self.resolver_intent,
            "resolverWarning": self.resolver_warning,
            "understandingStatus": self.understanding_status,
            "understandingWarning": self.understanding_warning,
            "memoryStatus": self.memory_status,
            "memoryWarning": self.memory_warning,
            "memoryDetail": self.memory_detail,
            "revisionStatus": self.revision_status,
            "revisionWarning": self.revision_warning,
            "droppedSections": self.dropped_sections,
            "warnings": self.warnings,
            "contextManifest": self.context_manifest,
            "sections": [section.as_dict() for section in self.sections],
            "systemInstruction": self.system_instruction,
            "messages": [message.as_dict() for message in self.messages],
        }


@dataclass(slots=True)
class _BuildScope:
    """一次 build 的局部台账。

    必须是 build 内部的局部对象，不能挂到 ContextBuilder 实例上：
    Runtime 持有一个 Builder 并可能并发处理多个请求，实例级可变状态会串数据。
    """

    plan: RetrievalPlan | None
    exclusions: list[dict[str, object]] = field(default_factory=list)
    allocations: dict[str, dict[str, object]] = field(default_factory=dict)
    effective_budget: int = 0


class ContextBuilder:
    def __init__(
        self,
        *,
        max_context_tokens: int = 3000,
        recent_history_limit: int = 12,
        budget_policy: BudgetPolicy | None = None,
        output_reserve_ratio: float | None = None,
        type_cap_ratios: dict[str, float] | None = None,
    ) -> None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须为正数")
        if recent_history_limit < 0:
            # 契约：0 = 不注入历史，负数非法。不要用 [-0:] 这种切片把 0 解释成全部。
            raise ValueError("recent_history_limit 不能为负数（0 表示不注入历史）")
        base = budget_policy or BudgetPolicy()
        policy = BudgetPolicy(
            output_reserve_ratio=(
                output_reserve_ratio
                if output_reserve_ratio is not None
                else base.output_reserve_ratio
            ),
            type_cap_ratios=(
                type_cap_ratios
                if type_cap_ratios is not None
                else dict(base.type_cap_ratios)
            ),
        )
        policy.validate()
        self._max_context_tokens = max_context_tokens
        self._recent_history_limit = recent_history_limit
        self._budget_policy = policy

    # ---------- 主流程 ----------

    def build(self, state: AgentState) -> BuiltContext:
        # D4-3：Plan 存在时，先按数据源与意图收敛候选，再按类型排序/定额/分层压缩。
        # Plan 为 None 只出现在直接构造 Builder 的单元测试与旧调用点，此时保留
        # 既有全局预算行为，避免把 D4-2 之前的契约测试改写成新语义。
        scope = _BuildScope(plan=state.retrieval_plan)
        sections = self._apply_plan_scope(self._collect_sections(state), scope)
        sections = self._dedupe(sections, scope)
        if scope.plan is not None:
            self._apply_type_budgets(sections, scope)
            self._degrade_redundant_sections(sections)
        self._fit_budget(sections, scope)

        system_instruction = self._render_system_instruction(sections, state)
        messages = self._render_messages(sections, system_instruction)

        # `_fit_budget` 只按 section 内容做一次快速裁剪；这里再按最终渲染结果
        # （标题、说明、实际消息，不含固定教学契约）收紧，直到动态上下文装得下。
        for _ in range(len(sections) + 2):
            context_tokens = _context_tokens(system_instruction, messages)
            if context_tokens <= self._max_context_tokens:
                break
            if not self._reduce_one(sections, scope):
                break
            system_instruction = self._render_system_instruction(sections, state)
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
            system_instruction = self._render_system_instruction(sections, state)
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
        self._finalize_injection_status(sections)
        manifest = self._build_manifest(sections, scope, context_tokens)
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
            evidence_status=state.evidence_status,
            evidence_warning=state.evidence_warning,
            cross_concept_evidence_status=state.cross_concept_evidence_status,
            cross_concept_evidence_warning=state.cross_concept_evidence_warning,
            resolver_status=state.resolver_status,
            resolver_intent=state.resolver_intent,
            resolver_warning=state.resolver_warning,
            understanding_status=state.understanding_status,
            understanding_warning=state.understanding_warning,
            memory_status=state.memory_status,
            memory_warning=state.memory_warning,
            memory_detail=state.memory_detail,
            revision_status=state.revision_status,
            revision_warning=state.revision_warning,
            context_manifest=manifest,
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
                # 不把用户消息前缀写进 source_id：Manifest 会复用这个字段，
                # 任何正文片段都不应进入可观测结构。
                source_ids=["message:current"],
                source_type="user_input",
                retrieval_status="SUCCESS",
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
                    source_ids=[f"history:{len(history) - distance}"],
                    source_type="history",
                    retrieval_status="SUCCESS",
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
                        source_type="session",
                        # Session 由请求上下文直接带入，不是后端检索结果；
                        # 它已经成功到手，因此状态是 SUCCESS，而不是 Policy 的
                        # REQUIRED / OPTIONAL / SKIP（那是检索需求，不是检索状态）。
                        retrieval_status="SUCCESS",
                        # Session 是整轮回答的框架，且通常只有一两行；
                        # 它被挤掉只是省几十 token，损失却是一整轮落点。
                        protected=True,
                    )
                )

        # 认知检索状态：只在检索失败/不完整时出现。它先于具体认知内容渲染，
        # 因为「这次没查成」会改变后面所有「没有看到」的解读方式。
        cognitive_status_note = _render_cognitive_status(state)
        if cognitive_status_note:
            sections.append(
                ContextSection(
                    name="cognitive_status",
                    title="学习认知检索状态",
                    content=cognitive_status_note,
                    priority=PRIORITY_COGNITIVE_STATUS,
                    source_ids=[
                        f"understanding-status:{state.understanding_status}",
                        f"memory-status:{state.memory_status}",
                        f"revision-status:{state.revision_status}",
                    ],
                    dedupe_key="cognitive_status",
                    source_type="cognitive_status",
                    retrieval_status=state.understanding_status,
                    protected=True,
                )
            )

        # 认知背景
        learning_memory_concept_ids = {
            memory.concept_id
            for memory in state.memories
            if memory.memory_type is MemoryType.LEARNING
        }
        # 每个 concept 只保留当前快照：先按 updated_at/id 倒序，重复的 concept
        # 由 _dedupe 保留第一条，后到的旧快照进入排除台账。
        for understanding in sorted(
            state.understandings,
            key=lambda item: (_time_key(item.updated_at), item.id),
            reverse=True,
        ):
            if not understanding.has_content and understanding.status is UnderstandingStatus.UNKNOWN:
                # 只有「UNKNOWN 且没有任何内容」的快照不提供任何信息，塞进去只会浪费预算。
                # EXPOSED / PARTIAL 等状态即使没有条目化正文也必须保留 ——
                # 「接触过但没写细节」本身就是一条有效事实，不能因为正文为空就整块丢掉。
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
                    source_type="understanding",
                    retrieval_status=state.understanding_status,
                    guard_text=_understanding_guard_text(understanding),
                    render_tier=lambda tier, item=understanding, conflict=has_learning_memory: (
                        _render_understanding(item, has_learning_memory=conflict, tier=tier)
                    ),
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
                    source_type="trajectory",
                    retrieval_status=state.revision_status,
                    render_tier=lambda tier, item=trajectory: _render_trajectory(
                        item, tier=tier
                    ),
                )
            )

        trajectory_evidence_ids = {
            evidence.id
            for trajectory in state.trajectories
            for evidence in trajectory.evidences
        }

        # Memory 先按作用域（CONCEPT > SPACE > GLOBAL），同作用域内按后端相关度
        # 的代理（importance）与更新时间。Python 排序是稳定的，因此没有这些字段时
        # 保留后端返回的相关度顺序。
        scope_priority = (
            state.retrieval_plan.memory_scope_priority
            if state.retrieval_plan is not None
            else ("CONCEPT", "SPACE", "GLOBAL")
        )
        for memory in sorted(
            state.memories,
            key=lambda item: _memory_sort_key(item, scope_priority),
        ):
            sections.append(
                ContextSection(
                    name=f"memory:{memory.id}",
                    title="长期记忆",
                    content=_render_memory(memory),
                    priority=PRIORITY_MEMORY,
                    source_ids=[f"memory:{memory.id}"],
                    dedupe_key=f"memory:{memory.id}",
                    source_type="memory",
                    retrieval_status=state.memory_status,
                    guard_text=memory.content or "",
                    render_tier=lambda tier, item=memory: _render_memory(item, tier=tier),
                )
            )

        concept_status_note = _render_concept_resolver_status(state)
        if concept_status_note:
            sections.append(
                ContextSection(
                    name="concept_status",
                    title="概念解析状态",
                    content=concept_status_note,
                    priority=PRIORITY_CONCEPT_STATUS,
                    source_ids=[f"resolver-status:{state.resolver_status}"],
                    dedupe_key="concept_status",
                    source_type="concept_status",
                    retrieval_status=state.resolver_status,
                    protected=True,
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
                    source_type="knowledge_status",
                    retrieval_status=state.knowledge_status,
                    protected=True,
                )
            )

        # 知识片段按检索相关度（score）排序；分数相同保留后端顺序。
        # 这里**不设 render_tier**：引用校验以 chunk 为最小单位，半个片段
        # 既不能用来源标注引用，也会让 injected/dropped 契约失真。
        for chunk in sorted(
            state.knowledge, key=lambda item: item.score or 0.0, reverse=True
        ):
            sections.append(
                ContextSection(
                    name=f"knowledge:{chunk.id}",
                    title="参考资料",
                    content=_render_knowledge(chunk),
                    priority=PRIORITY_KNOWLEDGE,
                    source_ids=[f"chunk:{chunk.id}"],
                    dedupe_key=f"chunk:{chunk.id}",
                    source_type="knowledge",
                    retrieval_status=state.knowledge_status,
                )
            )

        evidence_status_note = _render_evidence_status(state)
        if evidence_status_note:
            sections.append(
                ContextSection(
                    name="evidence_status",
                    title="本轮证据检索状态",
                    content=evidence_status_note,
                    priority=PRIORITY_EVIDENCE_STATUS,
                    source_ids=[
                        f"evidence-status:{state.evidence_status}",
                        f"cross-evidence-status:{state.cross_concept_evidence_status}",
                    ],
                    dedupe_key="evidence_status",
                    source_type="evidence_status",
                    retrieval_status=state.evidence_status,
                    protected=True,
                )
            )

        intent = state.retrieval_plan.intent if state.retrieval_plan else None
        # Evidence 始终是历史材料：先按意图相关性（复盘/追问时错误与解释类优先），
        # 再按时间倒序。排序只决定「谁先被保留」，不会把它渲染成当前状态。
        for evidence in sorted(
            state.evidences,
            key=lambda item: (
                _evidence_intent_relevance(item, intent),
                _time_key(item.created_at),
            ),
            reverse=True,
        ):
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
                        # 与原始证据共用同一 dedupe_key：同一条物理证据无论带不带
                        # 跨概念标记，都只允许注入一次。
                        dedupe_key=f"evidence:{evidence.id}",
                        source_type="related_evidence",
                        retrieval_status=state.cross_concept_evidence_status,
                        guard_text=evidence.observation or "",
                        render_tier=lambda tier, item=evidence: _render_related_evidence(
                            item, tier=tier
                        ),
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
                    source_type="evidence",
                    retrieval_status=state.evidence_status,
                    guard_text=evidence.observation or "",
                    render_tier=lambda tier, item=evidence: _render_evidence(
                        item, tier=tier
                    ),
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

    # ---------- 2. 按 Plan 收敛候选 ----------

    def _apply_plan_scope(
        self, sections: list[ContextSection], scope: _BuildScope
    ) -> list[ContextSection]:
        """把「本轮没查 / 不该查」的数据源挡在渲染之前。

        Runtime 已经按 Plan 决定是否发起检索；这里再兜一层，是为了让
        「state 里恰好有旧数据、但本轮 Plan 是 SKIP」这种情况不会把旧数据
        静默注入。Plan 为 None 时完全保留旧行为。
        """
        plan = scope.plan
        if plan is None:
            return sections
        kept: list[ContextSection] = []
        for section in sections:
            kind = _kind(section)
            if section.name == "user_input" or kind in {"history", "session"}:
                kept.append(section)
                continue
            if kind in PROTECTED_KINDS:
                kept.append(section)
                continue
            if kind == "trajectory" and plan.intent is not Intent.LEARNING_REVIEW:
                self._record_exclusion(
                    scope, section, INJECTION_EXCLUDED, REASON_NOT_REVIEW_INTENT
                )
                continue
            if _plan_need(plan, kind) is RetrievalNeed.SKIP:
                self._record_exclusion(
                    scope, section, INJECTION_EXCLUDED, REASON_POLICY_SKIP
                )
                continue
            kept.append(section)
        return kept

    # ---------- 3. 去重 ----------

    def _dedupe(
        self, sections: list[ContextSection], scope: _BuildScope
    ) -> list[ContextSection]:
        """同一 dedupe_key 只保留优先级最高的一份；重复项进入排除台账。"""
        seen: dict[str, ContextSection] = {}
        result: list[ContextSection] = []
        for section in sorted(sections, key=lambda item: item.priority, reverse=True):
            key = section.dedupe_key
            if key is None:
                result.append(section)
                continue
            if key in seen:
                self._record_exclusion(
                    scope, section, INJECTION_EXCLUDED, REASON_DUPLICATE_SOURCE
                )
                continue
            seen[key] = section
            result.append(section)
        return result

    # ---------- 4. 按类型额度 / 分层压缩 ----------

    def _apply_type_budgets(
        self, sections: list[ContextSection], scope: _BuildScope
    ) -> None:
        """REQUIRED 先分、OPTIONAL 只用剩余；每类先满文本，再 structured / one_line。

        固定与 protected 区块（本轮输入、Session、检索状态说明）先占预算；
        历史消息按「最新优先 + 类型上限」分配；弹性类型按优先级竞争。
        """
        plan = scope.plan
        if plan is None:
            return
        total = self._max_context_tokens
        effective = self._budget_policy.effective_total(total)
        scope.effective_budget = effective
        remaining = effective

        user_input = next(
            (section for section in sections if section.name == "user_input"), None
        )
        if user_input is not None:
            remaining = max(0, remaining - user_input.estimated_tokens)
        for section in sections:
            if section.protected and not section.dropped:
                remaining = max(0, remaining - section.estimated_tokens)

        remaining = self._allocate_history(sections, scope, effective, remaining)

        required: list[str] = []
        optional: list[str] = []
        for kind in ELASTIC_KIND_ORDER:
            need = _plan_need(plan, kind)
            if need is RetrievalNeed.REQUIRED:
                required.append(kind)
            elif need is RetrievalNeed.OPTIONAL:
                optional.append(kind)

        for group in (required, optional):
            for kind in group:
                cap = self._budget_policy.cap(kind, effective)
                used = 0
                for section in [
                    item
                    for item in sections
                    if _kind(item) == kind and not item.dropped
                ]:
                    available = max(0, min(cap - used, remaining))
                    cost = self._select_with_tiers(section, available)
                    if cost is None:
                        section.dropped = True
                        section.exclusion_reason = REASON_TYPE_BUDGET
                        continue
                    used += cost
                    remaining = max(0, remaining - cost)
                scope.allocations[kind] = {
                    "cap": cap,
                    "used": used,
                    "required": kind in required,
                }

    def _allocate_history(
        self,
        sections: list[ContextSection],
        scope: _BuildScope,
        effective: int,
        remaining: int,
    ) -> int:
        history = sorted(
            (
                section
                for section in sections
                if _kind(section) == "history" and not section.dropped
            ),
            key=_history_index,
            reverse=True,
        )
        cap = self._budget_policy.cap("history", effective)
        used = 0
        for index, section in enumerate(history):
            cost = section.estimated_tokens
            # 最新一轮允许越过类型上限一次：它是续接指代的最近锚点。
            within_cap = index == 0 or used + cost <= cap
            if within_cap and cost <= remaining:
                used += cost
                remaining -= cost
                continue
            section.dropped = True
            section.exclusion_reason = REASON_TYPE_BUDGET
        scope.allocations["history"] = {
            "cap": cap,
            "used": used,
            "required": False,
        }
        return remaining

    @staticmethod
    def _select_with_tiers(section: ContextSection, available: int) -> int | None:
        """在 available 内选择最详细的档位；返回 None 表示只能不注入。"""
        if section.estimated_tokens <= available:
            return section.estimated_tokens
        if section.render_tier is None:
            # Knowledge 等原子类型：不切正文，整段保留或不注入。
            return None
        for tier in (TIER_STRUCTURED, TIER_ONE_LINE):
            content = section.render_tier(tier)
            cost = estimate_tokens(content)
            if cost <= available:
                section.content = content
                section.estimated_tokens = cost
                section.tier = tier
                section.truncated = True
                section.compression_reason = f"{REASON_TIER_PREFIX}{tier}"
                return cost
        return None

    def _degrade_redundant_sections(self, sections: list[ContextSection]) -> None:
        """语义冗余只降细节，不删除；冲突内容不合并。

        只处理 Memory / Evidence / Related Evidence 与当前 Understanding 的重叠：
        当前快照已经完整表达同一事实时，历史材料降为 structured，仍然保留
        来源与时间，供模型核对，而不是把两边合并成一条新事实。
        """
        understanding_sections = [
            section
            for section in sections
            if _kind(section) == "understanding" and not section.dropped
        ]
        understanding_text = " ".join(
            section.content for section in understanding_sections
        )
        if not understanding_text:
            return
        # 守卫规则看「实质内容」，不看渲染骨架：见 ContextSection.guard_text 说明。
        understanding_guard_text = " ".join(
            section.guard_text or section.content
            for section in understanding_sections
        )
        for section in sections:
            if section.dropped:
                continue
            if _kind(section) not in {"memory", "evidence", "related_evidence"}:
                continue
            if section.tier != TIER_FULL or section.render_tier is None:
                continue
            if not _is_semantically_redundant(
                section.content,
                understanding_text,
                item_guard_text=section.guard_text or section.content,
                current_guard_text=understanding_guard_text,
            ):
                continue
            content = section.render_tier(TIER_STRUCTURED)
            cost = estimate_tokens(content)
            if cost >= section.estimated_tokens:
                continue
            section.content = content
            section.estimated_tokens = cost
            section.tier = TIER_STRUCTURED
            section.truncated = True
            section.compression_reason = REASON_SEMANTIC_REDUNDANCY

    # ---------- 5. 全局控 Token ----------

    def _fit_budget(
        self, sections: list[ContextSection], scope: _BuildScope
    ) -> None:
        """按优先级从高到低装，装不下的丢。

        protected 区块（Session / 状态说明）先占预算，不参与竞争；`user_input`
        永不丢弃。保留 `allow_truncate` 的字符级兜底，用于分层压缩之后仍超总预算
        的极端情况。
        """
        remaining = self._max_context_tokens
        for section in sections:
            if section.protected and not section.dropped:
                remaining -= section.estimated_tokens
        remaining = max(0, remaining)
        for section in sorted(sections, key=lambda item: item.priority, reverse=True):
            if section.dropped or section.protected:
                continue
            if section.estimated_tokens <= remaining:
                remaining -= section.estimated_tokens
                continue
            if section.allow_truncate and remaining >= MIN_USEFUL_SECTION_TOKENS:
                section.content = truncate_to_tokens(section.content, remaining)
                section.truncated = True
                section.compression_reason = (
                    section.compression_reason or REASON_CHAR_TRUNCATED
                )
                section.estimated_tokens = estimate_tokens(section.content)
                remaining -= section.estimated_tokens
                continue
            # 本轮输入即使超预算也保留：模型看不到问题，这次回答就没有意义
            if section.name == "user_input":
                section.content = truncate_to_tokens(section.content, max(remaining, 1))
                section.truncated = True
                section.compression_reason = REASON_CHAR_TRUNCATED
                section.estimated_tokens = estimate_tokens(section.content)
                remaining = 0
                continue
            section.dropped = True
            section.exclusion_reason = REASON_GLOBAL_BUDGET

    def _reduce_one(
        self, sections: list[ContextSection], scope: _BuildScope
    ) -> bool:
        """按优先级从低到高再收紧一段；返回 False 表示已无可删内容。

        优先截断允许截断的区块，其次整段丢弃。`user_input` 与 protected
        区块永不丢弃 —— 模型看不到问题/状态说明，这次回答就没有意义。
        """
        candidates = [
            section
            for section in sections
            if not section.dropped
            and not section.protected
            and section.name != "user_input"
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
            section.compression_reason = (
                section.compression_reason or REASON_CHAR_TRUNCATED
            )
            if section.estimated_tokens < before:
                return True
        section.dropped = True
        section.exclusion_reason = REASON_GLOBAL_BUDGET
        return True

    # ---------- 6. Manifest ----------

    @staticmethod
    def _record_exclusion(
        scope: _BuildScope,
        section: ContextSection,
        injection_status: str,
        reason: str,
    ) -> None:
        scope.exclusions.append(
            {
                "type": section.source_type or _kind(section),
                "source_id": (
                    section.source_ids[0] if section.source_ids else section.name
                ),
                "retrieval_status": section.retrieval_status or "UNKNOWN",
                "injection_status": injection_status,
                "reason": reason,
                "estimated_tokens": section.estimated_tokens,
            }
        )

    @staticmethod
    def _finalize_injection_status(sections: list[ContextSection]) -> None:
        for section in sections:
            if section.dropped:
                reason = section.exclusion_reason or REASON_GLOBAL_BUDGET
                section.injection_status = (
                    INJECTION_OMITTED
                    if reason in {REASON_TYPE_BUDGET, REASON_GLOBAL_BUDGET}
                    else INJECTION_EXCLUDED
                )
            elif section.truncated:
                section.injection_status = INJECTION_TRUNCATED
            else:
                section.injection_status = INJECTION_SELECTED

    def _build_manifest(
        self,
        sections: list[ContextSection],
        scope: _BuildScope,
        context_tokens: int,
    ) -> dict[str, object]:
        """最小 Manifest：只含 source_id / 状态 / 原因 / token 估算。

        严禁放入正文、完整 prompt、query 或异常原文。来源被排除时也留下记录，
        否则「没进上下文」的原因只能靠猜。
        """
        entries = [self._manifest_entry(section) for section in sections]
        entries.extend(scope.exclusions)
        entries.sort(key=lambda item: (str(item["type"]), str(item["source_id"])))
        per_type: dict[str, dict[str, object]] = {}
        kinds = sorted(
            {str(item["type"]) for item in entries} | set(scope.allocations)
        )
        for kind in kinds:
            items = [item for item in entries if item["type"] == kind]
            if not items:
                continue
            selected = [
                item
                for item in items
                if item["injection_status"] in {INJECTION_SELECTED, INJECTION_TRUNCATED}
            ]
            absent = [
                item
                for item in items
                if item["injection_status"] not in {INJECTION_SELECTED, INJECTION_TRUNCATED}
            ]
            if selected and absent:
                type_status = INJECTION_PARTIAL
            elif selected:
                type_status = INJECTION_SELECTED
            elif any(
                item["reason"]
                in {REASON_POLICY_SKIP, REASON_NOT_REVIEW_INTENT, REASON_DUPLICATE_SOURCE}
                for item in absent
            ):
                type_status = INJECTION_EXCLUDED
            else:
                type_status = INJECTION_OMITTED
            allocation = scope.allocations.get(kind, {})
            per_type[kind] = {
                "cap": allocation.get("cap"),
                "used": sum(int(item["estimated_tokens"]) for item in selected),
                "required": bool(
                    allocation.get(
                        "required",
                        kind in PROTECTED_KINDS
                        or kind in {"user_input", "history", "session"},
                    )
                ),
                "injection_status": type_status,
            }

        total = self._max_context_tokens
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "policy_version": scope.plan.policy_version if scope.plan else None,
            "intent": scope.plan.intent.value if scope.plan else None,
            "sources": entries,
            "budget": {
                "total": total,
                "effective": scope.effective_budget or total,
                "used": context_tokens,
                "remaining": max(0, total - context_tokens),
                "output_reserve": self._budget_policy.reserve_tokens(total),
                "per_type": per_type,
            },
        }

    @staticmethod
    def _manifest_entry(section: ContextSection) -> dict[str, object]:
        return {
            "type": section.source_type or _kind(section),
            "source_id": section.source_ids[0] if section.source_ids else section.name,
            "retrieval_status": section.retrieval_status or "UNKNOWN",
            "injection_status": section.injection_status or INJECTION_SELECTED,
            "reason": (
                section.exclusion_reason
                or section.compression_reason
                or REASON_WITHIN_BUDGET
            ),
            "estimated_tokens": section.estimated_tokens,
        }

    # ---------- 4/5. 渲染 ----------

    def _render_system_instruction(self, sections: list[ContextSection], state: AgentState) -> str:
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
        cognitive_kinds = {
            "session",
            "cognitive_status",
            "understanding",
            "trajectory",
            "memory",
        }
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
            degraded_notes: list[str] = []
            if _understanding_degraded(state):
                degraded_notes.append("本轮学习认知检索未完成或失败")
            if _memory_degraded(state):
                degraded_notes.append("本轮记忆检索未完成或失败")
            if _revision_degraded(state):
                degraded_notes.append("本轮历史轨迹检索未完成或失败")
            if _evidence_degraded(state):
                degraded_notes.append("本轮证据检索发生降级")
            if _concept_resolver_degraded(state):
                # Resolver 降级只说明「语义解析没完成」，规则仍可能漏掉概念；
                # 它绝不能等价于「用户没有相关历史」。
                degraded_notes.append("本轮概念语义解析未完成，规则识别可能漏掉概念")
            if degraded_notes:
                blocks.append(
                    "## 学习者背景\n"
                    f"（本次没有可用于回答的历史认知记录；但{'；'.join(degraded_notes)}，"
                    "不能据此判断学习者没有相关历史，也不要声称用户没学过、没有记录或过去没有误区。"
                    "按新知识谨慎教学即可。）"
                )
            elif _cognitive_not_queried(state):
                # 本轮根本没有发起这类检索（通常是没有识别到可关联概念）。
                # 此时不渲染任何「学习者背景」：沉默是中性的，而任何
                # 「没有记录」措辞都会被模型读成「用户没学过」。也避免为
                # 一次未发生的检索占用本就不宽裕的上下文预算。
                pass
            elif _understandings_without_content(state):
                blocks.append(
                    "## 学习者背景\n"
                    "（本轮检索到了认知记录，但没有可用于教学的详细内容；"
                    "这不代表用户从未学过，也不代表用户没有相关历史。）"
                )
            elif not _all_cognitive_queried(state):
                blocks.append(
                    "## 学习者背景\n"
                    "（本轮部分认知检索未发起，无法确认用户是否已有相关记录；"
                    "这不代表用户没有相关历史或记忆，不要声称用户没学过或没有记录。"
                    "按新知识谨慎教学即可。）"
                )
            else:
                # 只有「确实查过且确实为空」才允许说没有记录。
                blocks.append(
                    "## 学习者背景\n"
                    "（本轮已完成学习认知与记忆检索，结果确实为空；按新知识正常教学。"
                    "不要据此推断用户在其他知识点上的历史或记忆。）"
                )
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
        # 只有「确实查过且结果为空」才能说没有可用快照。
        # NOT_QUERIED 是「没查」，DEGRADED 是「没查成」—— 两者都绝不能写成
        # 「没有检索到 Understanding」，否则 meta/preview 会把失败说成事实。
        if _understanding_degraded(state):
            warnings.append(
                state.understanding_warning
                or "学习认知检索降级：无法确认用户是否已有认知记录，不能当作没有。"
            )
        elif state.understanding_status == "EMPTY" and not any(
            understanding.has_content for understanding in state.understandings
        ):
            warnings.append(
                "学习认知检索完成，结果为空：用户在这些知识点上确实没有可用快照。"
            )
        if _memory_degraded(state):
            warnings.append(
                state.memory_warning
                or "记忆检索降级：无法确认是否有相关记忆，不能当作没有。"
            )
        if _revision_degraded(state):
            warnings.append(
                state.revision_warning
                or "历史轨迹检索降级：无法确认用户过去是否存在误区或变化，不能当作没有历史。"
            )
        # 知识检索的正常空结果 / 阈值过滤 / 依赖失败由检索层给出明确原因。
        # 这里只负责把它带进 context preview 与 meta，不把失败伪装成「没有资料」。
        if state.knowledge_warning:
            warnings.append(state.knowledge_warning)
        # 相关概念查询失败必须与「没有相关概念」区分开：前者是依赖失败，
        # 后者是正常的空结果。这里只把检索层给出的失败原因带出来。
        if state.related_concepts_warning:
            warnings.append(state.related_concepts_warning)
        # 证据检索降级必须进入 meta / preview 的 warnings，不能只留在 trace 里。
        if state.evidence_warning:
            warnings.append(state.evidence_warning)
        if state.cross_concept_evidence_warning:
            warnings.append(state.cross_concept_evidence_warning)
        # 概念解析降级必须与「策略决定不调用」区分：前者是依赖失败，
        # 后者是正常的 E1 行为。只有降级/失败才进入 warnings。
        if _concept_resolver_degraded(state):
            detail = state.resolver_warning or "未提供具体原因"
            warnings.append(
                f"概念语义解析未完成（{state.resolver_status}），本轮仅使用规则识别结果；"
                f"不能据此判断用户没有相关历史。底层记录：{detail}"
            )
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
    "cognitive_status": (
        "本轮学习认知检索状态",
        "> 这决定本轮能否把「没有看到记录」当成「用户没有记录」。"
        "NOT_QUERIED 表示本轮未发起检索，DEGRADED 表示检索失败或不完整；"
        "两者都不说明用户没有学过、没有记忆或过去没有误区。",
    ),
    "understanding": (
        "学习者的当前认知（系统长期记录）",
        "> 这是系统对「这个用户现在怎么理解这些概念」的判断，用来决定**从哪讲起**。"
        "只有「已经理解」明确列出的内容才算已掌握；状态「已能讲清」不覆盖未列出的相关知识，"
        "「部分理解」「接触过」「未接触」都不能说成已掌握。"
        "只有状态、没有条目化内容也是一条有效记录：表示用户接触过该概念，"
        "但不能据此推断任何具体掌握程度。"
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
    "concept_status": (
        "本轮概念解析状态",
        "> 这决定本轮能否确定用户指的是哪些概念。解析降级只说明语义识别未完成，"
        "不说明用户没有相关历史；规则识别结果仍然有效，只是可能漏掉同义或模糊指代。",
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
    "evidence_status": (
        "本轮证据检索状态",
        "> 这决定本轮能不能把「没有看到证据」当成「没有历史证据」。"
        "降级只说明检索失败或不完整，不说明用户没有相关经历。",
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


def _plan_need(plan: RetrievalPlan, kind: str) -> RetrievalNeed | None:
    """把 Builder 的 section 类型映射回 Plan 的 need；未知类型不设限。"""
    decision = {
        "understanding": plan.understanding,
        "memory": plan.memory,
        "knowledge": plan.knowledge,
        "evidence": plan.evidence,
        "related_evidence": plan.evidence_related,
        "trajectory": plan.revision,
    }.get(kind)
    return decision.need if decision is not None else None


def _time_key(value: datetime | None) -> float:
    """可比较的时间键。缺失时间排在最旧；aware/naive 都走 timestamp。"""
    return value.timestamp() if value is not None else 0.0


def _history_index(section: ContextSection) -> int:
    try:
        return int(section.name.split(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _memory_sort_key(
    memory: MemoryItem, scope_priority: tuple[str, ...] | list[str]
) -> tuple[int, float, float]:
    """CONCEPT > SPACE > GLOBAL；同作用域内 importance / updated_at 优先。

    后端 hybrid 检索已经给过相关度顺序；这里没有显式 relevance 字段，
    因此用 importance 作为相关度代理，并把输入顺序留作稳定排序的兜底。
    """
    rank = {str(scope): index for index, scope in enumerate(scope_priority)}
    scope_rank = rank.get(memory.scope_type.value, len(rank))
    importance = float(memory.importance) if memory.importance is not None else 0.0
    return (scope_rank, -importance, -_time_key(memory.updated_at))


_EVIDENCE_TYPE_RELEVANCE = {
    EvidenceType.USER_EXPLANATION: 3,
    EvidenceType.USER_CORRECTION: 3,
    EvidenceType.TEST_RESULT: 2,
    EvidenceType.OBSERVATION: 1,
    EvidenceType.HIGHLIGHT_SIGNAL: 0,
}


def _evidence_intent_relevance(
    evidence: EvidenceItem, intent: Intent | None
) -> int:
    """意图相关性的确定性代理。

    复盘时「答错 / 部分正确」是追溯纠正的第一手材料；其余意图先按证据类型
    （用户解释 > 测试结果 > 观察 > 批注信号）排序，再在同一档内按时间倒序。
    """
    if intent is Intent.LEARNING_REVIEW:
        return (
            3
            if evidence.correctness
            in {EvidenceCorrectness.INCORRECT, EvidenceCorrectness.PARTIAL}
            else 1
        )
    return _EVIDENCE_TYPE_RELEVANCE.get(evidence.evidence_type, 1)


_OVERLAP_IGNORABLE = set(
    " \t\n，。；：、,.!?！？（）()[]【】「」“”‘’\"'-—｜|>#*`/\\"
)


def _normalize_overlap_text(text: str) -> str:
    stripped = text.strip()
    # 去掉 section 的 `- [标签]` 前缀：标签本身不该把重叠比例拉低。
    if stripped.startswith("- ["):
        close = stripped.find("]")
        if close != -1:
            stripped = stripped[close + 1 :].lstrip(" ｜")
    return "".join(ch.casefold() for ch in stripped if ch not in _OVERLAP_IGNORABLE)


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


# ---------- KI-01：字符重合之外的保护性规则 ----------
# 阈值 0.6 本身是 D4-3 的契约，不在这里改；下面只在「重合度高」之上加否决条件。
# 宁可漏判冗余（只少省一点预算），也不要把语义相反的记录压成摘要。
_NEGATION_MARKERS = (
    "不等于", "不属于", "不表示", "不成立", "不认为", "不是", "并非", "并不",
    "没有", "没能", "不会", "不能", "不要", "不再", "从不", "从未",
    "尚未", "未能", "不具", "不含", "未", "没", "无", "非", "不",
)
# 这些词含「不 / 未 / 非」但不是否定判断。先屏蔽再扫描，避免把「不同」
# 当成一次否定；漏掉真正的否定只会更保守，不会造成误压缩。
_NEGATION_NEUTRAL_COMPOUNDS = (
    "不同", "不断", "不仅", "不过", "不久", "不少", "部分", "非常", "未来", "无论",
    # 数学 / 学习语境里的名词，含「不」但不是否定判断。
    "不等式", "不确定",
)
_DATE_TIME_PATTERN = re.compile(
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?"
)
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_ENTITY_PATTERN = re.compile(r"[A-Z][A-Z0-9_-]{1,}")
_QUOTED_PATTERN = re.compile(r"《[^》]*》|「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’")


def _negation_markers(text: str) -> set[str]:
    cleaned = text
    for compound in _NEGATION_NEUTRAL_COMPOUNDS:
        cleaned = cleaned.replace(compound, "□")
    return {marker for marker in _NEGATION_MARKERS if marker in cleaned}


def _number_tokens(text: str) -> set[str]:
    # 渲染文本里的「记录于 2026-01-01」「更新于 ...」是元数据，不是实质数字差异。
    return set(_NUMBER_PATTERN.findall(_DATE_TIME_PATTERN.sub(" ", text)))


def _entity_tokens(text: str) -> set[str]:
    return set(_ENTITY_PATTERN.findall(text)) | set(_QUOTED_PATTERN.findall(text))


def _has_protective_difference(item_text: str, current_text: str) -> bool:
    """否定 / 数字 / 实体任一集合不同 → 保护，不判冗余。"""
    return (
        _negation_markers(item_text) != _negation_markers(current_text)
        or _number_tokens(item_text) != _number_tokens(current_text)
        or _entity_tokens(item_text) != _entity_tokens(current_text)
    )


def _is_semantically_redundant(
    item_text: str,
    current_text: str,
    *,
    item_guard_text: str | None = None,
    current_guard_text: str | None = None,
) -> bool:
    """保守的语义冗余判定：整段包含或字符 bigram 重合 ≥ 0.6。

    只用于「降低历史材料细节」，不用于删除；阈值刻意偏高，宁可漏判，
    也不要把只是同主题的 Memory / Evidence 当成当前认知的重复。

    KI-01 保护规则：字符重合之外再看否定词 / 数字 / 实体。任何一项不同都
    直接否决冗余判定。守卫使用调用方传入的实质内容（``guard_text``），
    避免把 Understanding 的固定说明文字（其中含「没有」「不要」）误当成
    事实层面的否定差异。
    """
    item = _normalize_overlap_text(item_text)
    current = _normalize_overlap_text(current_text)
    if len(item) < 8 or len(current) < 8:
        return False
    if _has_protective_difference(
        item_guard_text if item_guard_text is not None else item_text,
        current_guard_text if current_guard_text is not None else current_text,
    ):
        return False
    if item in current:
        return True
    item_grams = _bigrams(item)
    if not item_grams:
        return False
    return len(item_grams & _bigrams(current)) / len(item_grams) >= 0.6


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


def _understanding_guard_text(understanding: Understanding) -> str:
    """语义冗余守卫用的实质内容：只拼事实字段，不拼渲染骨架。"""
    parts = [understanding.concept_name or "", understanding.summary or ""]
    parts.extend(understanding.known_parts)
    parts.extend(item.text for item in understanding.misconceptions)
    parts.extend(understanding.uncertain_parts)
    if understanding.latest_user_explanation:
        parts.append(understanding.latest_user_explanation)
    return " ".join(part for part in parts if part)


def _render_understanding(
    understanding: Understanding,
    *,
    has_learning_memory: bool = False,
    tier: str = TIER_FULL,
) -> str:
    label = _STATUS_LABEL.get(understanding.status, understanding.status.value)
    lines = [f"### {understanding.concept_name} ｜ 状态：{label}"]
    if understanding.updated_at is not None:
        # 当前快照也必须带时间：否则与 Learning Memory 冲突时，模型无法判断
        # 哪一边更早，只能靠区块标题猜。
        lines.append(f"- 当前快照更新于 {_format_time(understanding.updated_at)}")
    if not understanding.has_content:
        # EXPOSED / PARTIAL 等状态可能没有任何条目化正文。整块丢掉会让模型
        # 把「接触过但没写细节」误读成「从未学过」，所以保留状态本身并明确边界。
        lines.append(
            "- 该知识点已有状态记录，但当前没有可用的详细内容"
            "（没有条目化的已理解 / 误区 / 不确定信息）。"
            "这只说明用户接触过该知识点，不能推断其具体掌握程度，"
            "也不能据此说用户从未学过。"
        )
    if tier == TIER_ONE_LINE:
        if understanding.summary:
            lines.append(f"- 当前理解：{_clip(understanding.summary, 60)}")
        if understanding.misconceptions:
            lines.append(
                f"- 当前误区：{_clip(understanding.misconceptions[0].text, 50)}"
            )
        if has_learning_memory:
            lines.append("- 长期记忆另有较早记录；以本条当前快照为准。")
        return "\n".join(lines)

    if understanding.summary:
        lines.append(f"- 当前理解：{understanding.summary}")
    known_parts = understanding.known_parts
    if tier == TIER_STRUCTURED:
        known_parts = known_parts[:3]
    if known_parts:
        lines.append(
            "- 已经理解（只有这个列表里的内容可以称为已掌握）："
            + "；".join(known_parts)
            + ("（其余条目因预算未展开）" if len(understanding.known_parts) > len(known_parts) else "")
        )
        if tier == TIER_FULL:
            lines.append(
                "- 这个列表之外的相关知识，即使本轮讲解提到，也不能说成用户已经掌握。"
            )
    misconceptions = understanding.misconceptions
    if tier == TIER_STRUCTURED:
        misconceptions = misconceptions[:3]
    if misconceptions:
        lines.append("- 仍然存在的误区（要针对性纠正，指出推理在哪一步分叉，而不是直接说「你错了」）：")
        for item in misconceptions:
            marker = "当前存在" if item.status is MisconceptionStatus.ACTIVE else "已有反向证据，可能正在解除"
            lines.append(f"  - [{marker}] {item.text}")
    uncertain_parts = understanding.uncertain_parts
    if tier == TIER_STRUCTURED:
        uncertain_parts = uncertain_parts[:3]
    if uncertain_parts:
        lines.append("- 还不确定（本次可以重点补）：" + "；".join(uncertain_parts))
    if understanding.latest_user_explanation:
        explanation = understanding.latest_user_explanation
        if tier == TIER_STRUCTURED:
            explanation = _clip(explanation, 80)
        lines.append(f"- 用户自己的说法：「{explanation}」")
    if has_learning_memory:
        lines.append(
            "- 注意：长期记忆里另有一条较早的学习状态记录，可能与本条当前认知不同；"
            "回答直接相关问题时，说明它是历史记录、当前状态已不同，不要把它当成本轮事实。"
        )
    # 这条规则在 Trajectory-enabled 与 Current-only 两种上下文里都必须存在，
    # 否则两种模式的非轨迹输入就不一致，C 的对照会失去意义。
    # 只有完整档在 section 内重复它；structured / one_line 由
    # BASE_SYSTEM_INSTRUCTION 中的同义规则兜底，避免预算紧张时重复占篇幅。
    if tier == TIER_FULL:
        lines.append(
            "- 如果本轮没有提供历史轨迹：不要从当前状态反推过去，也不要把"
            "「没有看到错误记录」说成「过去没有错误」；用户问历史时说明当前可见信息不足。"
        )
    return "\n".join(lines)


def _render_memory(memory: MemoryItem, *, tier: str = TIER_FULL) -> str:
    label = _MEMORY_LABEL.get(memory.memory_type, memory.memory_type.value)
    scope = _MEMORY_SCOPE_LABEL.get(memory.scope_type, memory.scope_type.value)
    notes: list[str] = []
    if memory.created_at:
        notes.append(f"记录于 {_format_time(memory.created_at)}")
    if memory.updated_at and memory.updated_at != memory.created_at:
        notes.append(f"更新于 {_format_time(memory.updated_at)}")
    if memory.memory_type is MemoryType.LEARNING and memory.concept_id is not None:
        # 不臆测「哪条一定冲突」，只明确边界：两边都保留，以当前快照为准。
        # 这样 Memory / Understanding 不会被合并成一条新事实。
        notes.append("历史记录，可能与当前认知冲突；两边均保留，以当前快照为准")
    if tier == TIER_ONE_LINE:
        notes.append("来源：长期记忆")
        content = _clip(memory.content, 50)
    elif tier == TIER_STRUCTURED:
        content = _clip_sentences(memory.content, 120)
    else:
        content = memory.content
    suffix = f"（{'；'.join(notes)}）" if notes else ""
    return f"- [{label}，作用域：{scope}] {content}{suffix}"


def _render_trajectory(
    trajectory: LearningTrajectory, *, tier: str = TIER_FULL
) -> str:
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

    if tier == TIER_ONE_LINE:
        historical = historical_misconceptions(trajectory)
        if trajectory.current_misconceptions:
            lines.append(
                "- 当前仍存在的误区："
                + _clip("；".join(trajectory.current_misconceptions), 50)
            )
        elif historical:
            lines.append(f"- 历史误区：{_clip(historical[0], 50)}")
        lines.append(
            f"- 历史版本 {len(trajectory.revisions)} 条、关联证据 "
            f"{len(trajectory.evidences)} 条；本次因预算仅保留摘要。"
        )
        return "\n".join(lines)

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

    if tier == TIER_FULL and trajectory.evidences:
        lines.append("- 关联证据（按时间）：")
        for evidence in trajectory.evidences:
            lines.append("  - " + _render_trajectory_evidence(evidence))
    elif tier == TIER_STRUCTURED and trajectory.evidences:
        lines.append(
            f"- 关联证据 {len(trajectory.evidences)} 条（本次仅保留数量，未展开细节）。"
        )

    if tier == TIER_FULL:
        lines.append(
            "- 以上是全部可核对记录；未记录的动机、原因、是否借助外部资料，"
            "均无法确认，不要补写。"
        )
    else:
        lines.append(
            "- 以上仅为预算压缩后的历史摘要；未展开的记录不等于不存在，"
            "不要据此断言用户过去没有相关变化。"
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


def _clip(text: str | None, limit: int) -> str:
    """one_line 档的确定性截断：保留前 limit 个字符，明确标记还有省略。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


# structured 档按句子边界切，而不是从字符中间切。句号/分号/换行是主要边界，
# 逗号只在「找不到更强边界」时兜底。
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。；;！!?？\n])")
_SENTENCE_BOUNDARIES = set("。；;！!?？\n，,")


def _split_sentences(text: str) -> list[str]:
    return [segment for segment in _SENTENCE_SPLIT_PATTERN.split(text) if segment]


def _has_protected_token(text: str) -> bool:
    """含否定词 / 数字 / 实体的句子，在超长时必须优先保留。"""
    return bool(
        _negation_markers(text)
        or _NUMBER_PATTERN.search(text)
        or _ENTITY_PATTERN.search(text)
        or _QUOTED_PATTERN.search(text)
    )


def _clip_sentences(text: str | None, limit: int) -> str:
    """structured 档截断：按句边界取舍，优先保留关键否定 / 数字 / 实体句。

    规则刻意保守：先选受保护句、首句、尾句，再用其余句子按原文顺序填满；
    输出保持原文顺序，省略处用「…」标记，绝不从句子中间切开。
    """
    value = (text or "").strip()
    if limit <= 0:
        return "…"
    if len(value) <= limit:
        return value
    segments = _split_sentences(value)
    total = len(segments)
    if total <= 1:
        return _cut_at_boundary(value, limit)

    def render(indices: set[int]) -> str:
        parts: list[str] = []
        previous: int | None = None
        for index in sorted(indices):
            if previous is not None and index != previous + 1:
                parts.append("…")
            parts.append(segments[index])
            previous = index
        if previous != total - 1:
            parts.append("…")
        return "".join(parts)

    selected: set[int] = set()
    # 否定句优先级最高：数字/实体可能出现在很多句子里（例如「第1条」「第2条」），
    # 若与否定句同档，前面的普通句子会把预算吃光，反而挤掉安全关键的否定。
    negation_priority = [
        index
        for index, segment in enumerate(segments)
        if _negation_markers(segment)
    ]
    other_priority = [
        index
        for index, segment in enumerate(segments)
        if index not in negation_priority and _has_protected_token(segment)
    ]
    priority = negation_priority + other_priority + [0, total - 1]
    for index in dict.fromkeys(priority):
        selected.add(index)
        if len(render(selected)) > limit:
            selected.discard(index)
    for index in range(total):
        if index in selected:
            continue
        selected.add(index)
        if len(render(selected)) > limit:
            selected.discard(index)
    if selected:
        rendered = render(selected)
        if len(rendered) <= limit:
            return rendered
    return _cut_at_boundary(value, limit)


def _cut_at_boundary(text: str, limit: int) -> str:
    """单句仍然超限时的最后兜底：在最近边界处切，找不到才退化为头截断。"""
    window = text[: max(1, limit - 1)]
    for index in range(len(window) - 1, -1, -1):
        if window[index] in _SENTENCE_BOUNDARIES:
            return window[: index + 1].rstrip() + "…"
    return window.rstrip() + "…"


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
            section.source_ids = [
                f"knowledge-status:{final_state}",
                f"knowledge-retrieval:{raw_status}",
            ]
            section.source_type = "knowledge_status"
            section.retrieval_status = final_state
            section.protected = True
            section.exclusion_reason = None
            section.compression_reason = None
            section.tier = TIER_FULL
            section.truncated = False
            section.dropped = False
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
            source_type="knowledge_status",
            retrieval_status=final_state,
            protected=True,
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


_COGNITIVE_DEGRADED = "DEGRADED"
_COGNITIVE_NOT_QUERIED = "NOT_QUERIED"
_COGNITIVE_QUERIED = {"EMPTY", "SUCCESS"}


def _understanding_degraded(state: AgentState) -> bool:
    return str(state.understanding_status) == _COGNITIVE_DEGRADED


def _memory_degraded(state: AgentState) -> bool:
    return str(state.memory_status) == _COGNITIVE_DEGRADED


def _revision_degraded(state: AgentState) -> bool:
    return str(state.revision_status) == _COGNITIVE_DEGRADED


def _cognitive_not_queried(state: AgentState) -> bool:
    """两类认知检索都没发起。空列表在这里只表示「没查」，不表示「没有」。"""
    return (
        str(state.understanding_status) == _COGNITIVE_NOT_QUERIED
        and str(state.memory_status) == _COGNITIVE_NOT_QUERIED
    )


def _all_cognitive_queried(state: AgentState) -> bool:
    return (
        str(state.understanding_status) in _COGNITIVE_QUERIED
        and str(state.memory_status) in _COGNITIVE_QUERIED
    )


def _understandings_without_content(state: AgentState) -> bool:
    """检索确实返回了快照，但没有任何可渲染的条目化内容。"""
    return bool(state.understandings) and not any(
        understanding.has_content for understanding in state.understandings
    )


def _render_cognitive_status(state: AgentState) -> str:
    """渲染学习认知 / 记忆 / 历史轨迹的检索状态。

    只为 DEGRADED 输出：SUCCESS / EMPTY 由内容本身说明，NOT_QUERIED 不产生
    任何「没有记录」的暗示。DEGRADED 的文案必须同时说清「失败」与
    「不能据此判断用户没有」，否则模型仍会顺手补一句「你没有相关记录」。
    """
    blocks: list[str] = []
    if _understanding_degraded(state):
        blocks.append(
            "\n".join(
                [
                    "- 学习认知检索：DEGRADED",
                    f"- 检索层记录：{state.understanding_warning or '未提供具体原因'}",
                    "- 说明：本轮学习认知检索失败或不完整，无法确认用户是否已有认知记录。"
                    "不要声称用户没学过、没有认知记录或这是全新概念；"
                    "需要时说明本轮未能完整检索。",
                ]
            )
        )
    if _memory_degraded(state):
        blocks.append(
            "\n".join(
                [
                    "- 长期记忆检索：DEGRADED",
                    f"- 检索层记录：{state.memory_warning or '未提供具体原因'}",
                    "- 说明：本轮记忆检索失败或不完整，不能据此判断用户没有相关记忆。"
                    "不要声称用户没有记忆或从未提过相关偏好；需要时说明本轮未能完整检索。",
                ]
            )
        )
    if _revision_degraded(state):
        blocks.append(
            "\n".join(
                [
                    "- 历史轨迹检索：DEGRADED",
                    f"- 检索层记录：{state.revision_warning or '未提供具体原因'}",
                    "- 说明：本轮历史版本检索失败或不完整，不能据此判断用户过去没有误区、"
                    "没有变化或没有历史记录。需要时说明本轮未能完整检索历史。",
                ]
            )
        )
    return "\n".join(blocks)


_EVIDENCE_DEGRADED = "DEGRADED"


def _evidence_degraded(state: AgentState) -> bool:
    return (
        str(state.evidence_status) == _EVIDENCE_DEGRADED
        or str(state.cross_concept_evidence_status) == _EVIDENCE_DEGRADED
    )


_CONCEPT_RESOLVER_DEGRADED = {"DEGRADED", "FAILED"}


def _concept_resolver_degraded(state: AgentState) -> bool:
    """只有 DEGRADED / FAILED 需要提示；NOT_CALLED 是正常策略，SUCCESS 无需解释。"""
    return str(state.resolver_status) in _CONCEPT_RESOLVER_DEGRADED


def _render_concept_resolver_status(state: AgentState) -> str:
    if not _concept_resolver_degraded(state):
        return ""
    return "\n".join(
        [
            f"- 状态：{state.resolver_status}",
            f"- 解析层记录：{state.resolver_warning or '未提供具体原因'}",
            "- 说明：本轮概念语义解析未完成，规则识别结果仍已使用；"
            "不能据此判断学习者没有相关历史或从未学过相关概念。"
            "不要声称用户没有记录、没有学过或没有相关历史。",
        ]
    )


def _render_evidence_status(state: AgentState) -> str:
    """只对 DEGRADED 输出解释；SUCCESS / EMPTY 不需要额外说明。

    这段文字必须同时做两件事：说明“检索失败”，并明确禁止把它当成
    “用户没有历史证据”。只写“检索失败”不够 —— 模型仍可能顺手补一句
    “你没有相关记录”，而那正是本批要消除的错误结论。
    """
    blocks: list[str] = []
    if str(state.evidence_status) == _EVIDENCE_DEGRADED:
        blocks.append(
            "\n".join(
                [
                    "- 原始概念证据检索：DEGRADED",
                    f"- 检索层记录：{state.evidence_warning or '未提供具体原因'}",
                    "- 说明：本轮证据检索部分失败，不能据此判断学习者没有相关历史。"
                    "不要声称用户没有证据、从未学过或没有相关记录；"
                    "如果必须回答，请说明本轮证据未能完整检索。",
                ]
            )
        )
    if str(state.cross_concept_evidence_status) == _EVIDENCE_DEGRADED:
        blocks.append(
            "\n".join(
                [
                    "- 跨概念证据检索：DEGRADED",
                    f"- 检索层记录：{state.cross_concept_evidence_warning or '未提供具体原因'}",
                    "- 说明：相关概念的证据检索失败或不完整；"
                    "不能据此判断这些相关概念没有历史证据，也不要断言用户未学过它们。",
                ]
            )
        )
    return "\n".join(blocks)


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


def _render_evidence(evidence, *, tier: str = TIER_FULL) -> str:
    label = _CORRECTNESS_LABEL.get(evidence.correctness, evidence.correctness.value)
    evidence_type = _EVIDENCE_TYPE_LABEL.get(
        evidence.evidence_type, evidence.evidence_type.value
    )
    when = _format_time(evidence.created_at)
    prefix = f"{when} ｜ " if when else ""
    if tier == TIER_ONE_LINE:
        return (
            f"- [{label}/{evidence_type}] {prefix}{_clip(evidence.observation, 50)}"
            "（来源：历史证据）"
        )
    if tier == TIER_STRUCTURED:
        return f"- [{label}/{evidence_type}] {prefix}{_clip_sentences(evidence.observation, 120)}"
    return f"- [{label}/{evidence_type}] {prefix}{evidence.observation}"


def _render_related_evidence(evidence, *, tier: str = TIER_FULL) -> str:
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
    observation = evidence.observation
    if tier == TIER_ONE_LINE:
        observation = _clip(observation, 50) + "（来源：相关概念历史证据）"
    elif tier == TIER_STRUCTURED:
        observation = _clip_sentences(observation, 120)
    return (
        f"- [{label}/{evidence_type}] 关联依据：{via}{source}"
        f" ｜ 证据归属概念：#{evidence.concept_id} ｜ {prefix}{observation}"
    )
