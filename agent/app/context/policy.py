"""D4-2 Retrieval Policy / Planner —— 决定「本轮查什么、注入哪类上下文」。

这一层刻意只做**检索前的决策**，不做排序、预算与渲染（那是 D4-3 的事）：

```text
用户消息 + 本轮 concept_ids + Session 挂靠结果 + 空间概念目录
        ↓  规则（不调用 LLM）
RetrievalPlan(intent, session, understanding, memory, evidence, revision, knowledge)
        ↓
Runtime 执行：按 plan 决定是否调用各 Retriever；按 session 决策决定是否注入 goal/focus
```

为什么需要它：这些判断以前散落在 `runtime._retrieve` 的词表和布尔条件里
（`_wants_evidence` / `_is_history_review` / `bool(concept_ids)` / Knowledge 每轮必查），
没有统一的意图、没有「为什么查」，也无法在 SessionResolver 误挂旧 Session 时
把旧 goal 挡在 prompt 之外。这里把它们收敛成纯函数，输出结构化 Plan。

**边界**（与 D4-1 审计的结论一致）：

- 不新增 Selector、不接入 LLM 意图分类、不新增中间件；
- 不改变 Retriever 协议与检索实现；
- `REQUIRED` / `OPTIONAL` 在本批都表示「发起检索」，区别只描述意图对数据源的
  依赖强度，供 D4-3 的预算分配与 D4-4 的 Manifest 使用；`SKIP` 才表示不发起。
- 没有任何概念锚点时，Understanding / Memory / Evidence / Revision 一律 SKIP：
  这些数据在后端以 `user_id + concept_id` 为锚，不能凭空检索；Knowledge 不受影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.retriever.concept_detector import detect_concepts
from app.schemas import Concept, HistoryMessage, SessionContext

POLICY_VERSION = "d4-2-v1"

# Memory 的作用域优先级。后端 hybrid 检索会同时返回可见的 GLOBAL / SPACE /
# CONCEPT 记忆；这里只声明「同一话题下谁更贴近当前问题」，不让 Policy 自行过滤，
# 因为可见性规则必须由后端 SQL 唯一实现。
MEMORY_SCOPE_PRIORITY = ("CONCEPT", "SPACE", "GLOBAL")


class Intent(str, Enum):
    """最小意图集合。规则版，后续可替换为 LLM 分类而不改 Plan 结构。"""

    LEARNING_EXPLANATION = "LEARNING_EXPLANATION"
    LEARNING_REVIEW = "LEARNING_REVIEW"
    CONTINUE_TASK = "CONTINUE_TASK"
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    META_QUESTION = "META_QUESTION"
    CASUAL = "CASUAL"


class RetrievalNeed(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    SKIP = "SKIP"


# ---------- 规则词表（从 runtime 迁出，语义保持不变） ----------

# Evidence 是「按需召回」的：只在用户质疑判断、要求解释依据或要复盘时才去捞。
EVIDENCE_TRIGGERS = (
    "为什么",
    "凭什么",
    "你怎么知道",
    "依据",
    "证据",
    "你确定",
    "确定吗",
    "搞错",
    "说错",
    "不对吧",
    "复盘",
    "回顾",
)

# 显式复盘意图：只有这些词命中时，才读取 Revision 历史并精确回查旧证据。
REVIEW_TRIGGERS = (
    "之前",
    "以前",
    "曾经",
    "过去",
    "后来",
    "纠正",
    "改过来",
    "错在哪",
    "哪里错",
    "哪一步错",
    "搞错",
    "说错",
    "误解",
    "复盘",
    "回顾",
    "学习轨迹",
)

# 个人历史问题的特征：要么是第一人称，要么直接问「错在哪 / 怎么纠正」。
_PERSONAL_HISTORY_MARKERS = ("我", "自己")
_HISTORY_ERROR_MARKERS = (
    "错在哪",
    "哪里错",
    "哪一步错",
    "错在",
    "错了",
    "纠正",
    "改过来",
    "搞错",
    "说错",
    "复盘",
    "回顾",
    "学习轨迹",
)

# 询问「自己的掌握程度 / 薄弱点」。刻意保持窄：只认明确的自我评估措辞，
# 避免把普通概念解释（含「理解」）误判成 META_QUESTION。
_META_MARKERS = (
    "掌握程度",
    "掌握得",
    "掌握怎么样",
    "薄弱",
    "弱点",
    "盲区",
    "学得怎么样",
    "学到哪",
    "学习进度",
    "进度如何",
    "我的水平",
    "我理解对",
    "理解得怎么样",
    "我还差",
    "哪里薄弱",
)

# 明确指向用户上传的教材 / 文档。命中即 KNOWLEDGE_QUERY，优先于普通解释。
_DOCUMENT_MARKERS = (
    "资料",
    "文档",
    "教材",
    "课本",
    "讲义",
    "笔记",
    "原文",
    "章节",
    "第几页",
    "书里",
    "文件中",
    "上传的",
    "pdf",
)

# 会话续接信号。必须配合「Session 存在且概念匹配」才会判成 CONTINUE_TASK；
# 单独出现「继续」但没有可匹配的会话时，仍按普通概念/知识问题处理。
_CONTINUATION_MARKERS = (
    "继续",
    "接着",
    "下一步",
    "刚才",
    "上面",
    "前面",
    "再讲",
    "再说",
    "还有呢",
    "然后呢",
)

# 纯寒暄 / 确认。只匹配「整句话都是这些词」的情况；不包含「继续 / 然后呢」，
# 否则它们永远无法进入 CONTINUE_TASK。与 evidence_guard 的寒暄判断保持同一方向，
# 但这里更窄：宁可把一句话当成知识问题，也不要用寒暄把用户真正的追问静默掉。
_CASUAL_ONLY_RE = re.compile(
    r"^(?:(?:你好|您好|hi|hello|嗨|在吗|谢谢|多谢|感谢|好的|好吧|好|嗯|哦|"
    r"再见|拜拜|收到|明白|懂了|辛苦了|随便聊聊|讲个笑话|你是谁|"
    r"你叫什么(?:名字)?|你能做什么)[\s，,。！!？?~啊呀吧呢哦]*)+$",
    re.IGNORECASE,
)


# ---------- Plan 结构 ----------


@dataclass(frozen=True, slots=True)
class SourceDecision:
    """一个数据源的检索决策。reason 是规则名，供 trace / D4-4 Manifest 复用。"""

    need: RetrievalNeed
    reason: str

    @property
    def triggered(self) -> bool:
        return self.need is not RetrievalNeed.SKIP

    def as_dict(self) -> dict[str, object]:
        return {"need": self.need.value, "triggered": self.triggered, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """Session 是否注入，以及注入哪几部分。

    `recent_turns` 在本批只表示「保留现有会话历史注入逻辑」；Runtime 不额外
    读取 Session 级的 recent turns（当前请求只带 Conversation history）。
    """

    need: RetrievalNeed
    inject_goal: bool
    inject_current_focus: bool
    inject_recent_turns: bool
    reason: str
    session_id: int | None = None
    # None = 无法判定（会话文本里没有可识别概念，或本轮没有概念）。
    concept_match: bool | None = None

    @property
    def injects_frame(self) -> bool:
        return self.inject_goal or self.inject_current_focus

    def as_dict(self) -> dict[str, object]:
        return {
            "need": self.need.value,
            "injectGoal": self.inject_goal,
            "injectCurrentFocus": self.inject_current_focus,
            "injectRecentTurns": self.inject_recent_turns,
            "reason": self.reason,
            "sessionId": self.session_id,
            "conceptMatch": self.concept_match,
        }


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """一次检索的结构化计划。Runtime 只执行，不在下游重新做触发判断。"""

    policy_version: str
    intent: Intent
    intent_reason: str
    concepts: tuple[int, ...]
    session: SessionDecision
    understanding: SourceDecision
    memory: SourceDecision
    memory_concept_ids: tuple[int, ...]
    evidence: SourceDecision
    evidence_related: SourceDecision
    revision: SourceDecision
    knowledge: SourceDecision
    review_required: bool
    memory_scope_priority: tuple[str, ...] = MEMORY_SCOPE_PRIORITY
    reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """结构化输出（不含任何正文），可直接进 trace 或未来的 Manifest。"""
        return {
            "policyVersion": self.policy_version,
            "intent": self.intent.value,
            "intentReason": self.intent_reason,
            "concepts": list(self.concepts),
            "reviewRequired": self.review_required,
            "session": self.session.as_dict(),
            "needs": {
                "understanding": self.understanding.as_dict(),
                "memory": self.memory.as_dict(),
                "evidence": self.evidence.as_dict(),
                "evidenceRelated": self.evidence_related.as_dict(),
                "revision": self.revision.as_dict(),
                "knowledge": self.knowledge.as_dict(),
            },
            "memoryConceptIds": list(self.memory_concept_ids),
            "memoryScopePriority": list(self.memory_scope_priority),
            "reasons": dict(self.reasons),
        }


# ---------- 入口 ----------


def plan_retrieval(
    *,
    user_message: str,
    concept_ids: list[int] | tuple[int, ...],
    session: SessionContext | None,
    space_concepts: list[Concept] | None = None,
    history: list[HistoryMessage] | None = None,
) -> RetrievalPlan:
    """规则型 Planner。纯函数，不读配置、不访问服务。

    ``space_concepts`` 是当前 Space 的概念目录，只用于判断 Session 文本里提到了
    哪些概念（不参与本轮概念识别）。``history`` 当前不参与决策，保留参数是为了
    与 Runtime 的调用形状对齐，并为未来「续接轮次」规则留出明确接缝。
    """
    del history  # 本批没有基于对话轮次的规则；显式保留参数而不做隐式读取。

    ids = tuple(dict.fromkeys(int(item) for item in concept_ids))
    session_concept_ids = _session_concept_ids(session, space_concepts or [])
    concept_match = _concept_match(session_concept_ids, ids)
    review_required = is_history_review(user_message)
    evidence_requested = wants_evidence(user_message)

    intent, intent_reason = _classify_intent(
        user_message,
        ids,
        review_required=review_required,
        session=session,
        session_concept_match=concept_match,
    )
    session_decision = _decide_session(session, intent, concept_match)
    decisions = _decide_sources(
        intent,
        has_concepts=bool(ids),
        evidence_requested=evidence_requested,
    )

    understanding = decisions["understanding"]
    memory = decisions["memory"]
    evidence = decisions["evidence"]
    revision = decisions["revision"]
    knowledge = decisions["knowledge"]

    # 跨概念扩展只跟随原始 Evidence 触发；具体上限仍由 Runtime 的配置决定。
    if evidence.triggered:
        evidence_related = SourceDecision(
            RetrievalNeed.OPTIONAL,
            f"follows_evidence:{evidence.reason}",
        )
    else:
        evidence_related = SourceDecision(
            RetrievalNeed.SKIP,
            f"no_evidence_query:{evidence.reason}",
        )

    reasons = {
        "intent": intent_reason,
        "session": session_decision.reason,
        "understanding": understanding.reason,
        "memory": memory.reason,
        "memoryConceptIds": (
            "all_detected_concepts"
            if memory.triggered and ids
            else "no_memory_query"
        ),
        "evidence": evidence.reason,
        "evidenceRelated": evidence_related.reason,
        "revision": revision.reason,
        "knowledge": knowledge.reason,
    }

    return RetrievalPlan(
        policy_version=POLICY_VERSION,
        intent=intent,
        intent_reason=intent_reason,
        concepts=ids,
        session=session_decision,
        understanding=understanding,
        memory=memory,
        memory_concept_ids=ids if memory.triggered else (),
        evidence=evidence,
        evidence_related=evidence_related,
        revision=revision,
        knowledge=knowledge,
        review_required=review_required,
        reasons=reasons,
    )


# ---------- 意图规则 ----------


def _classify_intent(
    user_message: str,
    concept_ids: tuple[int, ...],
    *,
    review_required: bool,
    session: SessionContext | None,
    session_concept_match: bool | None,
) -> tuple[Intent, str]:
    """按固定优先级返回意图与规则名。第一条命中即停。"""
    if review_required:
        return Intent.LEARNING_REVIEW, "personal_history_review"
    if _has_marker(user_message, _META_MARKERS):
        return Intent.META_QUESTION, "meta_marker"
    if _has_marker(user_message, _DOCUMENT_MARKERS):
        return Intent.KNOWLEDGE_QUERY, "document_reference"
    if _CASUAL_ONLY_RE.match((user_message or "").strip()):
        return Intent.CASUAL, "casual_only_message"
    if (
        session is not None
        and session_concept_match is True
        and _has_marker(user_message, _CONTINUATION_MARKERS)
    ):
        return Intent.CONTINUE_TASK, "session_concept_match+continuation"
    if concept_ids:
        return Intent.LEARNING_EXPLANATION, "concept_hit_explanation"
    return Intent.KNOWLEDGE_QUERY, "no_concept_substantive_question"


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    probe = (text or "").casefold()
    return any(marker.casefold() in probe for marker in markers)


def wants_evidence(query: str) -> bool:
    """用户是否在明确要求解释判断依据 / 复盘。"""
    return _has_marker(query, EVIDENCE_TRIGGERS)


def is_history_review(query: str) -> bool:
    """是否显式个人复盘。

    需要同时命中「时间 / 纠错类复盘词」和「第一人称或明确的纠错问法」。
    只看「以前」会把「过去十年 CPU 架构有什么变化」误判成个人复盘。
    """
    if not _has_marker(query, REVIEW_TRIGGERS):
        return False
    return _has_marker(query, _PERSONAL_HISTORY_MARKERS) or _has_marker(
        query, _HISTORY_ERROR_MARKERS
    )


# ---------- Session 决策 ----------


def _session_concept_ids(
    session: SessionContext | None,
    space_concepts: list[Concept],
) -> tuple[int, ...]:
    """会话文本里可识别的概念 id。

    复用规则版 `detect_concepts`，与 SessionResolver 规则 2 的「文本命中」
    口径一致；不引入第二套匹配逻辑。只取文本命中（传空 history）。
    """
    if session is None or not space_concepts:
        return ()
    text = " ".join(
        part for part in (session.title, session.goal, session.current_focus) if part
    )
    if not text.strip():
        return ()
    detected = detect_concepts(text, [], space_concepts)
    return tuple(dict.fromkeys(concept.id for concept, _, _ in detected))


def _concept_match(
    session_concept_ids: tuple[int, ...],
    concept_ids: tuple[int, ...],
) -> bool | None:
    """会话与本轮概念是否匹配。

    只有「两边都有概念且完全不相交」才是确定的 mismatch。任一侧为空时，
    没有证据说明是新话题 —— 保持注入比误删一个真实学习目标更安全。
    """
    if not session_concept_ids or not concept_ids:
        return None
    return bool(set(session_concept_ids) & set(concept_ids))


def _decide_session(
    session: SessionContext | None,
    intent: Intent,
    concept_match: bool | None,
) -> SessionDecision:
    if session is None:
        return SessionDecision(
            RetrievalNeed.SKIP,
            inject_goal=False,
            inject_current_focus=False,
            inject_recent_turns=True,
            reason="no_session",
            session_id=None,
            concept_match=None,
        )

    if intent is Intent.CASUAL:
        # 寒暄不该被上一段学习活动的旧目标改写落点。保留历史轮次，
        # 但不注入 goal / current_focus。
        return SessionDecision(
            RetrievalNeed.SKIP,
            inject_goal=False,
            inject_current_focus=False,
            inject_recent_turns=True,
            reason="casual_no_task_frame",
            session_id=session.id,
            concept_match=concept_match,
        )

    if concept_match is False:
        # D4-2C 缺口：SessionResolver 规则 3 可能把新话题挂到旧 Session。
        # 当两边都有明确概念且不相交时，旧 goal/focus 不再适用于本轮。
        return SessionDecision(
            RetrievalNeed.OPTIONAL,
            inject_goal=False,
            inject_current_focus=False,
            inject_recent_turns=True,
            reason="session_concept_mismatch",
            session_id=session.id,
            concept_match=False,
        )

    has_frame = bool((session.goal or "").strip() or (session.current_focus or "").strip())
    if not has_frame:
        return SessionDecision(
            RetrievalNeed.OPTIONAL,
            inject_goal=False,
            inject_current_focus=False,
            inject_recent_turns=True,
            reason="session_without_goal",
            session_id=session.id,
            concept_match=concept_match,
        )

    return SessionDecision(
        RetrievalNeed.REQUIRED,
        inject_goal=bool((session.goal or "").strip()),
        inject_current_focus=bool((session.current_focus or "").strip()),
        inject_recent_turns=True,
        reason=(
            "session_concept_match"
            if concept_match is True
            else "session_frame_undetermined"
        ),
        session_id=session.id,
        concept_match=concept_match,
    )


# ---------- 数据源矩阵 ----------


def _decide_sources(
    intent: Intent,
    *,
    has_concepts: bool,
    evidence_requested: bool,
) -> dict[str, SourceDecision]:
    """意图 × 数据源矩阵。

    `REQUIRED` / `OPTIONAL` 在本批都触发检索；`SKIP` 不触发。
    没有概念锚点时统一覆盖为 SKIP —— U / M / E / Revision 都按 concept 存储，
    不能在没有 concept_id 的情况下发起「全量历史」查询。
    """
    if intent is Intent.LEARNING_REVIEW:
        decisions = {
            "understanding": SourceDecision(
                RetrievalNeed.REQUIRED, "review_reads_current_understanding"
            ),
            "memory": SourceDecision(
                RetrievalNeed.REQUIRED, "review_reads_learning_memory"
            ),
            # Revision 是复盘的第一手数据；原始 Evidence 只在用户显式要求依据时
            # 才作为「没有 Revision 时」的兜底。否则「我之前错在哪」会在没有轨迹时
            # 把最近几条证据拼成一段并不存在的复盘叙事，且增加首字延迟。
            "evidence": (
                SourceDecision(
                    RetrievalNeed.REQUIRED, "explicit_evidence_request"
                )
                if evidence_requested
                else SourceDecision(
                    RetrievalNeed.SKIP, "review_uses_revision_not_recent_evidence"
                )
            ),
            "revision": SourceDecision(
                RetrievalNeed.REQUIRED, "review_requires_revision_history"
            ),
            "knowledge": SourceDecision(
                RetrievalNeed.OPTIONAL, "review_knowledge_optional"
            ),
        }
    elif intent is Intent.META_QUESTION:
        decisions = {
            "understanding": SourceDecision(
                RetrievalNeed.REQUIRED, "meta_reads_current_understanding"
            ),
            "memory": SourceDecision(
                RetrievalNeed.REQUIRED, "meta_reads_learning_memory"
            ),
            "evidence": SourceDecision(
                RetrievalNeed.REQUIRED, "meta_requires_evidence"
            ),
            # 掌握程度用「当前快照 + 证据」回答；完整版本历史留给显式复盘。
            "revision": SourceDecision(
                RetrievalNeed.SKIP, "meta_uses_current_snapshot"
            ),
            "knowledge": SourceDecision(
                RetrievalNeed.OPTIONAL, "meta_knowledge_optional"
            ),
        }
    elif intent is Intent.KNOWLEDGE_QUERY:
        decisions = {
            "understanding": SourceDecision(
                RetrievalNeed.OPTIONAL, "knowledge_query_may_use_understanding"
            ),
            "memory": SourceDecision(
                RetrievalNeed.OPTIONAL, "knowledge_query_may_use_memory"
            ),
            "evidence": (
                SourceDecision(
                    RetrievalNeed.REQUIRED, "explicit_evidence_request"
                )
                if evidence_requested
                else SourceDecision(
                    RetrievalNeed.SKIP, "knowledge_query_no_evidence_request"
                )
            ),
            "revision": SourceDecision(
                RetrievalNeed.SKIP, "knowledge_query_no_revision"
            ),
            "knowledge": SourceDecision(
                RetrievalNeed.REQUIRED, "knowledge_query_requires_documents"
            ),
        }
    elif intent is Intent.CONTINUE_TASK:
        decisions = {
            "understanding": SourceDecision(
                RetrievalNeed.REQUIRED, "continue_reads_current_understanding"
            ),
            "memory": SourceDecision(
                RetrievalNeed.REQUIRED, "continue_reads_learning_memory"
            ),
            "evidence": (
                SourceDecision(
                    RetrievalNeed.REQUIRED, "explicit_evidence_request"
                )
                if evidence_requested
                else SourceDecision(
                    RetrievalNeed.SKIP, "continue_no_evidence_request"
                )
            ),
            "revision": SourceDecision(
                RetrievalNeed.SKIP, "continue_no_revision"
            ),
            "knowledge": SourceDecision(
                RetrievalNeed.OPTIONAL, "continue_knowledge_optional"
            ),
        }
    elif intent is Intent.CASUAL:
        decisions = {
            "understanding": SourceDecision(
                RetrievalNeed.SKIP, "casual_no_cognitive_retrieval"
            ),
            "memory": SourceDecision(
                RetrievalNeed.SKIP, "casual_no_cognitive_retrieval"
            ),
            "evidence": SourceDecision(
                RetrievalNeed.SKIP, "casual_no_evidence"
            ),
            "revision": SourceDecision(
                RetrievalNeed.SKIP, "casual_no_revision"
            ),
            "knowledge": SourceDecision(
                RetrievalNeed.SKIP, "casual_no_knowledge_retrieval"
            ),
        }
    else:  # LEARNING_EXPLANATION
        decisions = {
            "understanding": SourceDecision(
                RetrievalNeed.REQUIRED, "explanation_reads_current_understanding"
            ),
            "memory": SourceDecision(
                RetrievalNeed.REQUIRED, "explanation_reads_learning_memory"
            ),
            "evidence": (
                SourceDecision(
                    RetrievalNeed.REQUIRED, "explicit_evidence_request"
                )
                if evidence_requested
                else SourceDecision(
                    RetrievalNeed.SKIP, "explanation_no_evidence_request"
                )
            ),
            "revision": SourceDecision(
                RetrievalNeed.SKIP, "explanation_no_revision"
            ),
            "knowledge": SourceDecision(
                RetrievalNeed.REQUIRED, "explanation_requires_documents"
            ),
        }

    if not has_concepts:
        for name in ("understanding", "memory", "evidence", "revision"):
            decisions[name] = SourceDecision(
                RetrievalNeed.SKIP,
                f"no_concepts:{decisions[name].reason}",
            )
    return decisions
