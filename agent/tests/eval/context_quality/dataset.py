"""D4-5 上下文质量评测数据集。

这里是**声明式 fixture**，不是生产数据，也不是真实模型输出：

- 概念目录、Understanding / Memory / Evidence / Knowledge / Revision 全部由本文件预置；
- 每个 case 都写明「预期检索」「预期注入 / 不注入」「预期回答约束」；
- 评测脚本只负责执行当前 `plan_retrieval` + `ContextBuilder`，不反过来迁就数据。

覆盖 10 类场景、每类至少 2 个 case（预算压力额外覆盖分层截断）：

1. 普通知识问答（LEARNING_EXPLANATION）
2. 同一 Session 连续学习（CONTINUE_TASK）
3. 跨 Session 继续学习（同概念，新 Session）
4. 复盘过去的错误（LEARNING_REVIEW）
5. 评估当前掌握程度（META_QUESTION）
6. 询问教材 / 文档内容（KNOWLEDGE_QUERY）
7. 知识主题切换（验证不污染）
8. 历史 Evidence 与当前 Understanding 不一致（保留双方）
9. 检索 DEGRADED / 部分数据不可访问（失败可见）
10. 预算压力（知识片段被裁、protected 区块超限）

所有时间都是固定值，所有文本标记都是确定性的，保证重复运行结果一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.schemas import (
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceType,
    HistoryMessage,
    KnowledgeChunk,
    MemoryItem,
    MemoryScopeType,
    MemoryType,
    MessageRole,
    Misconception,
    MisconceptionStatus,
    SessionContext,
    Understanding,
    UnderstandingRevision,
    UnderstandingStatus,
)

T0 = datetime(2026, 1, 1, 9, 0)
T1 = datetime(2026, 1, 2, 9, 0)
T2 = datetime(2026, 1, 5, 9, 0)
T3 = datetime(2026, 1, 10, 9, 0)
T4 = datetime(2026, 2, 1, 9, 0)
T5 = datetime(2026, 2, 2, 9, 0)


def concept(
    concept_id: int,
    canonical_name: str,
    display_name: str | None = None,
) -> Concept:
    return Concept(
        id=concept_id,
        canonical_name=canonical_name,
        display_name=display_name or canonical_name,
    )


CALCULUS = (
    concept(101, "极限"),
    concept(102, "导数"),
    concept(103, "连续性"),
)
OS = (
    concept(201, "页表"),
    concept(202, "TLB", "TLB（快表）"),
    concept(203, "缺页"),
)
NETWORK = (
    concept(301, "TCP"),
    concept(302, "拥塞控制"),
)
CATALOG = (*CALCULUS, *OS, *NETWORK)


def u(
    understanding_id: int,
    concept_id: int,
    concept_name: str,
    *,
    summary: str,
    known_parts: tuple[str, ...] = (),
    uncertain_parts: tuple[str, ...] = (),
    misconceptions: tuple[Misconception, ...] = (),
    latest_user_explanation: str | None = None,
    status: UnderstandingStatus = UnderstandingStatus.PARTIAL,
    updated_at: datetime | None = T2,
) -> Understanding:
    return Understanding(
        id=understanding_id,
        concept_id=concept_id,
        concept_name=concept_name,
        summary=summary,
        known_parts=list(known_parts),
        uncertain_parts=list(uncertain_parts),
        misconceptions=list(misconceptions),
        latest_user_explanation=latest_user_explanation,
        status=status,
        updated_at=updated_at,
    )


def misc(text: str, status: MisconceptionStatus = MisconceptionStatus.ACTIVE) -> Misconception:
    return Misconception(text=text, status=status)


def m(
    memory_id: int,
    content: str,
    *,
    concept_id: int | None = None,
    scope: MemoryScopeType = MemoryScopeType.CONCEPT,
    memory_type: MemoryType = MemoryType.LEARNING,
    importance: float | None = 0.5,
    created_at: datetime | None = T0,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        scope_type=scope,
        learning_space_id=1,
        concept_id=concept_id,
        memory_type=memory_type,
        content=content,
        importance=importance,
        created_at=created_at,
        updated_at=created_at,
    )


def e(
    evidence_id: int,
    concept_id: int,
    observation: str,
    *,
    correctness: EvidenceCorrectness = EvidenceCorrectness.UNKNOWN,
    evidence_type: EvidenceType = EvidenceType.OBSERVATION,
    created_at: datetime | None = T1,
    cross: bool = False,
    source_concept_id: int | None = None,
    related_via: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        concept_id=concept_id,
        evidence_type=evidence_type,
        observation=observation,
        correctness=correctness,
        created_at=created_at,
        is_cross_concept=cross,
        source_concept_id=source_concept_id,
        related_via=related_via,
    )


def k(
    chunk_id: int,
    content: str,
    *,
    document_name: str = "导数讲义.pdf",
    chunk_index: int = 0,
    score: float = 0.9,
    heading_path: tuple[str, ...] = (),
    page_number: int | None = None,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id,
        document_id=chunk_id,
        chunk_index=chunk_index,
        document_name=document_name,
        file_type="pdf",
        heading_path=list(heading_path),
        content=content,
        score=score,
        page_number=page_number,
    )


def rev(
    revision_id: int,
    concept_id: int,
    revision_no: int,
    *,
    summary: str,
    misconceptions: tuple[Misconception, ...] = (),
    status: UnderstandingStatus = UnderstandingStatus.PARTIAL,
    trigger_evidence_id: int | None = None,
    created_at: datetime | None = T0,
) -> UnderstandingRevision:
    return UnderstandingRevision(
        id=revision_id,
        concept_id=concept_id,
        revision_no=revision_no,
        summary=summary,
        misconceptions=list(misconceptions),
        status=status,
        trigger_evidence_id=trigger_evidence_id,
        created_at=created_at,
    )


@dataclass(frozen=True)
class Retrieved:
    """Fake Retriever 可注入的预置检索结果。"""

    understandings: tuple[Understanding, ...] = ()
    understanding_status: str = "EMPTY"
    understanding_warning: str | None = None

    memories: tuple[MemoryItem, ...] = ()
    memory_status: str = "EMPTY"
    memory_detail: str | None = None
    memory_warning: str | None = None

    knowledge: tuple[KnowledgeChunk, ...] = ()
    knowledge_status: str = "NO_MATCH"
    knowledge_warning: str | None = None

    evidences: tuple[EvidenceItem, ...] = ()
    evidence_status: str = "EMPTY"
    evidence_warning: str | None = None

    related_evidence: tuple[EvidenceItem, ...] = ()
    related_evidence_status: str = "EMPTY"
    related_evidence_warning: str | None = None

    revisions: dict[int, tuple[UnderstandingRevision, ...]] = field(default_factory=dict)
    revision_status: str = "EMPTY"
    revision_warning: str | None = None
    evidence_details: tuple[EvidenceItem, ...] = ()


@dataclass(frozen=True)
class Expected:
    """一个 case 的预期。injection 的 key 是 Manifest 的 source_id。"""

    intent: str
    retrieval: dict[str, str]

    session_inject_goal: bool | None = None
    session_inject_focus: bool | None = None
    session_concept_match: bool | None = None
    session_reason: str | None = None

    # source_id -> SELECTED / TRUNCATED / OMITTED / EXCLUDED / INJECTED / ABSENT
    injection: dict[str, str] = field(default_factory=dict)
    # 非 SELECTED 的关键原因；SELECTED 默认 within_budget。
    # 例：policy_skip / intent_not_review / type_budget / tier:structured。
    injection_reasons: dict[str, str] = field(default_factory=dict)
    must_not_inject: tuple[str, ...] = ()
    # source_id -> 渲染文本中必须与该 source 的注入状态一致的标记
    markers: dict[str, str] = field(default_factory=dict)
    # section 名称 -> 必须包含 / 不得包含的文本
    section_contains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    section_excludes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    required_text: tuple[str, ...] = ()
    forbidden_text: tuple[str, ...] = ()
    degraded_sources: tuple[str, ...] = ()
    personalization_sources: tuple[str, ...] = ()
    knowledge_state: str | None = None
    allow_known_overflow: bool = False


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    description: str
    user_message: str
    concept_ids: tuple[int, ...]
    retrieved: Retrieved
    expected: Expected
    session: SessionContext | None = None
    history: tuple[HistoryMessage, ...] = ()
    max_context_tokens: int = 3000
    recent_history_limit: int = 12
    output_reserve_ratio: float | None = None
    # 为测试 Builder 的防御层：这些类型的数据即使本轮 Plan 是 SKIP，也强行塞进 state。
    inject_unrequested: tuple[str, ...] = ()


def _history(*pairs: tuple[str, str]) -> tuple[HistoryMessage, ...]:
    return tuple(
        HistoryMessage(
            role=MessageRole.USER if role == "user" else MessageRole.ASSISTANT,
            content=content,
        )
        for role, content in pairs
    )


# =====================================================================
# 1. 普通知识问答（LEARNING_EXPLANATION）
# =====================================================================


_EXPLAIN_DERIV_U = u(
    500,
    102,
    "导数",
    summary="UND-DERIV 用户把导数理解为瞬时变化率，但还不能从极限定义推导。",
    known_parts=("导数是瞬时变化率",),
    status=UnderstandingStatus.PARTIAL,
    updated_at=T2,
)
_EXPLAIN_DERIV_M = m(
    400,
    "MEM-DERIV 用户偏好先看几何直觉，再看 ε-δ 形式定义。",
    concept_id=102,
)
_EXPLAIN_DERIV_K = k(
    700,
    "KNW-DERIV 导数定义：函数在某点的瞬时变化率，记作 f'(x)。",
    document_name="导数讲义.pdf",
)


CASES: tuple[Case, ...] = (
    Case(
        case_id="explain-derivative-definition",
        category="learning_explanation",
        description="普通解释：读取当前认知与长期记忆，不查 Evidence / Revision。",
        user_message="导数的定义是什么？",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(_EXPLAIN_DERIV_U,),
            understanding_status="SUCCESS",
            memories=(_EXPLAIN_DERIV_M,),
            memory_status="SUCCESS",
            knowledge=(_EXPLAIN_DERIV_K,),
            knowledge_status="SUCCESS",
            evidences=(
                e(
                    1021,
                    102,
                    "STALE-EVID-DERIV 用户曾把导数当作平均变化率。",
                    correctness=EvidenceCorrectness.INCORRECT,
                ),
            ),
            evidence_status="SUCCESS",
            revisions={
                102: (
                    rev(
                        700,
                        102,
                        1,
                        summary="TRAJ-DERIV 早期版本",
                        misconceptions=(misc("TRAJ-DERIV 把导数当成平均变化率"),),
                        status=UnderstandingStatus.MISCONCEPTION,
                        created_at=T0,
                    ),
                )
            },
            revision_status="SUCCESS",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "chunk:700": "SELECTED",
                "evidence:1021": "EXCLUDED",
                "revision:700": "EXCLUDED",
            },
            injection_reasons={
                "evidence:1021": "policy_skip",
                "revision:700": "intent_not_review",
            },
            markers={
                "understanding:500": "UND-DERIV",
                "memory:400": "MEM-DERIV",
                "chunk:700": "KNW-DERIV",
                "evidence:1021": "STALE-EVID-DERIV",
                "revision:700": "TRAJ-DERIV",
            },
            required_text=(
                "## 学习者的当前认知",
                "## 这个用户的长期记忆",
                "## 参考资料",
            ),
            forbidden_text=("没有历史记录", "用户没有相关历史"),
            personalization_sources=("understanding:500", "memory:400"),
        ),
        inject_unrequested=("evidence", "trajectory"),
    ),
    Case(
        case_id="explain-limit-epsilon-delta",
        category="learning_explanation",
        description="普通解释：Memory 为空、知识检索 NO_MATCH，空结果不得写成「资料不存在」。",
        user_message="极限的 ε-δ 定义怎么理解？",
        concept_ids=(101,),
        retrieved=Retrieved(
            understandings=(
                u(
                    501,
                    101,
                    "极限",
                    summary="UND-LIMIT 用户理解极限是无限趋近，但没接触过 ε-δ 语言。",
                    known_parts=("极限描述无限趋近",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memory_status="EMPTY",
            knowledge=(),
            knowledge_status="NO_MATCH",
            knowledge_warning="知识检索没有命中可用片段。",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:501": "SELECTED",
                "knowledge-status:NO_MATCH": "SELECTED",
            },
            markers={
                "understanding:501": "UND-LIMIT",
                "knowledge-status:NO_MATCH": "NO_MATCH",
            },
            required_text=(
                "## 学习者的当前认知",
                "## 本轮知识检索状态",
                "没有找到可用的文档片段",
            ),
            forbidden_text=("资料中不存在", "用户没有相关历史"),
            personalization_sources=("understanding:501",),
            knowledge_state="NO_MATCH",
        ),
    ),
)


# =====================================================================
# 2. 同一 Session 连续学习（CONTINUE_TASK）
# =====================================================================


CASES += (
    Case(
        case_id="continue-derivative-session",
        category="continue_task_same_session",
        description="同一 Session 续接：Session 与概念匹配，注入 goal / focus。",
        user_message="继续，刚才讲的导数我还没懂",
        concept_ids=(102,),
        session=SessionContext(
            id=10,
            title="导数与极限",
            goal="SESSION-GOAL-DERIV 理解导数的几何意义",
            current_focus="SESSION-FOCUS-DERIV 极限到导数的过渡",
        ),
        retrieved=Retrieved(
            understandings=(_EXPLAIN_DERIV_U,),
            understanding_status="SUCCESS",
            memories=(_EXPLAIN_DERIV_M,),
            memory_status="SUCCESS",
            knowledge=(_EXPLAIN_DERIV_K,),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="CONTINUE_TASK",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "OPTIONAL",
            },
            session_inject_goal=True,
            session_inject_focus=True,
            session_concept_match=True,
            session_reason="session_concept_match",
            injection={
                "session:10": "SELECTED",
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "chunk:700": "SELECTED",
            },
            markers={
                "session:10": "SESSION-GOAL-DERIV",
                "understanding:500": "UND-DERIV",
                "memory:400": "MEM-DERIV",
                "chunk:700": "KNW-DERIV",
            },
            required_text=(
                "## 本阶段学习目标",
                "## 学习者的当前认知",
                "## 这个用户的长期记忆",
            ),
            forbidden_text=("没有历史记录",),
            personalization_sources=("understanding:500", "memory:400"),
        ),
    ),
    Case(
        case_id="continue-tlb-session",
        category="continue_task_same_session",
        description="同一 Session 续接：带上一轮对话，读取 TLB 当前认知与记忆。",
        user_message="接着讲下一步，TLB 命中流程",
        concept_ids=(202,),
        session=SessionContext(
            id=11,
            title="TLB 快表",
            goal="SESSION-GOAL-TLB 掌握 TLB 命中与未命中流程",
            current_focus="SESSION-FOCUS-TLB 命中路径",
        ),
        history=_history(
            ("user", "TLB 是什么？"),
            ("assistant", "TLB 是页表项的缓存。"),
        ),
        retrieved=Retrieved(
            understandings=(
                u(
                    502,
                    202,
                    "TLB（快表）",
                    summary="UND-TLB 用户知道 TLB 是页表项的缓存。",
                    known_parts=("TLB 是页表项缓存",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    401,
                    "MEM-TLB 用户偏好用地址翻译流程图理解 TLB。",
                    concept_id=202,
                ),
            ),
            memory_status="SUCCESS",
            knowledge=(
                k(
                    701,
                    "KNW-TLB TLB 命中时无需再次访问页表。",
                    document_name="操作系统讲义.pdf",
                ),
            ),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="CONTINUE_TASK",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "OPTIONAL",
            },
            session_inject_goal=True,
            session_inject_focus=True,
            session_concept_match=True,
            session_reason="session_concept_match",
            injection={
                "session:11": "SELECTED",
                "understanding:502": "SELECTED",
                "memory:401": "SELECTED",
                "chunk:701": "SELECTED",
            },
            markers={
                "session:11": "SESSION-GOAL-TLB",
                "understanding:502": "UND-TLB",
                "memory:401": "MEM-TLB",
                "chunk:701": "KNW-TLB",
            },
            required_text=(
                "## 本阶段学习目标",
                "## 学习者的当前认知",
                "## 这个用户的长期记忆",
            ),
            personalization_sources=("understanding:502", "memory:401"),
        ),
    ),
)


# =====================================================================
# 3. 跨 Session 继续学习（同概念，新 Session）
# =====================================================================


CASES += (
    Case(
        case_id="cross-session-derivative-new-session",
        category="cross_session_learning",
        description="新 Session 但概念相同：仍应读取上一阶段的当前认知与记忆。",
        user_message="我们新开了一个会话，继续学导数",
        concept_ids=(102,),
        session=SessionContext(
            id=20,
            title="导数练习",
            goal="SESSION-GOAL-NEW 完成导数练习",
            current_focus="SESSION-FOCUS-NEW 链式法则",
        ),
        retrieved=Retrieved(
            understandings=(
                u(
                    500,
                    102,
                    "导数",
                    summary="UND-DERIV-XS 用户已经理解导数的几何意义。",
                    known_parts=("导数是切线斜率",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T3,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    400,
                    "MEM-DERIV-XS 用户偏好先看几何直觉。",
                    concept_id=102,
                ),
            ),
            memory_status="SUCCESS",
            knowledge=(_EXPLAIN_DERIV_K,),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="CONTINUE_TASK",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "OPTIONAL",
            },
            session_inject_goal=True,
            session_inject_focus=True,
            session_concept_match=True,
            session_reason="session_concept_match",
            injection={
                "session:20": "SELECTED",
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "chunk:700": "SELECTED",
            },
            markers={
                "session:20": "SESSION-GOAL-NEW",
                "understanding:500": "UND-DERIV-XS",
                "memory:400": "MEM-DERIV-XS",
                "chunk:700": "KNW-DERIV",
            },
            required_text=("## 本阶段学习目标", "## 学习者的当前认知"),
            personalization_sources=("understanding:500", "memory:400"),
        ),
    ),
    Case(
        case_id="cross-session-derivative-no-session",
        category="cross_session_learning",
        description="新会话没有 Session 归属：没有 goal/focus，但仍读取同概念的认知与记忆。",
        user_message="继续学导数，上次讲到链式法则",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(
                u(
                    500,
                    102,
                    "导数",
                    summary="UND-DERIV-XS2 用户上次已经理解链式法则。",
                    known_parts=("链式法则",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T3,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    400,
                    "MEM-DERIV-XS2 用户希望先做例题再总结。",
                    concept_id=102,
                ),
            ),
            memory_status="SUCCESS",
            knowledge=(_EXPLAIN_DERIV_K,),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "chunk:700": "SELECTED",
            },
            markers={
                "understanding:500": "UND-DERIV-XS2",
                "memory:400": "MEM-DERIV-XS2",
                "chunk:700": "KNW-DERIV",
            },
            required_text=("## 学习者的当前认知", "## 这个用户的长期记忆"),
            forbidden_text=("没有历史记录",),
            personalization_sources=("understanding:500", "memory:400"),
        ),
    ),
)


# =====================================================================
# 4. 复盘过去的错误（LEARNING_REVIEW）
# =====================================================================


_REVIEW_DERIV_U = u(
    500,
    102,
    "导数",
    summary="UND-REVIEW-DERIV 用户已经纠正链式法则误区。",
    known_parts=("链式法则",),
    status=UnderstandingStatus.PARTIAL,
    updated_at=T3,
)
_REVIEW_DERIV_M = m(
    400,
    "MEM-REVIEW-DERIV 用户习惯用内外层函数拆解复合函数。",
    concept_id=102,
)
_REVIEW_DERIV_REVISIONS = {
    102: (
        rev(
            700,
            102,
            1,
            summary="早期版本",
            misconceptions=(
                misc("TRAJ-REVIEW-DERIV 把链式法则内外函数顺序弄反"),
            ),
            status=UnderstandingStatus.MISCONCEPTION,
            trigger_evidence_id=1021,
            created_at=T0,
        ),
        rev(
            701,
            102,
            2,
            summary="已纠正",
            status=UnderstandingStatus.PARTIAL,
            trigger_evidence_id=1022,
            created_at=T3,
        ),
    )
}
_REVIEW_DERIV_EVIDENCE = (
    e(
        1021,
        102,
        "EVID-REVIEW-DERIV 用户把内层函数当外层求导。",
        correctness=EvidenceCorrectness.INCORRECT,
        created_at=T0,
    ),
    e(
        1022,
        102,
        "EVID-REVIEW-DERIV-2 用户后来正确写出链式法则。",
        correctness=EvidenceCorrectness.CORRECT,
        created_at=T3,
    ),
)


CASES += (
    Case(
        case_id="review-derivative-misconception",
        category="learning_review",
        description="显式复盘：读取 Revision 历史并精确回查关联 Evidence，不取最近 Evidence。",
        user_message="我之前对导数链式法则的理解错在哪里？",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(_REVIEW_DERIV_U,),
            understanding_status="SUCCESS",
            memories=(_REVIEW_DERIV_M,),
            memory_status="SUCCESS",
            evidences=_REVIEW_DERIV_EVIDENCE,
            evidence_status="SUCCESS",
            revisions=_REVIEW_DERIV_REVISIONS,
            revision_status="SUCCESS",
            evidence_details=_REVIEW_DERIV_EVIDENCE,
        ),
        expected=Expected(
            intent="LEARNING_REVIEW",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "REQUIRED",
                "knowledge": "OPTIONAL",
            },
            injection={
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "revision:700": "SELECTED",
            },
            markers={
                "understanding:500": "UND-REVIEW-DERIV",
                "memory:400": "MEM-REVIEW-DERIV",
                "revision:700": "TRAJ-REVIEW-DERIV",
            },
            section_contains={
                "trajectory:102": (
                    "已纠正，历史",
                    "EVID-REVIEW-DERIV",
                ),
                "understanding:102": ("UND-REVIEW-DERIV",),
            },
            section_excludes={
                "understanding:102": ("TRAJ-REVIEW-DERIV",),
            },
            required_text=(
                "## 学习者的当前认知",
                "## 学习轨迹（历史记录，不等于当前状态）",
                "当前状态以「学习者的当前认知」区块为准",
            ),
            forbidden_text=("你现在仍然把链式法则", "过去没有误区"),
            personalization_sources=(
                "understanding:500",
                "memory:400",
                "revision:700",
            ),
        ),
    ),
    Case(
        case_id="review-continuity-with-evidence-request",
        category="learning_review",
        description="复盘 + 显式依据请求：Revision 与 Evidence 都触发，历史与当前分开。",
        user_message="复盘一下我之前把连续性和极限搞混的过程，依据是什么？",
        concept_ids=(103,),
        retrieved=Retrieved(
            understandings=(
                u(
                    503,
                    103,
                    "连续性",
                    summary="UND-REVIEW-CONT 用户已能区分连续性与极限。",
                    known_parts=("连续要求函数值等于极限值",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T3,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    402,
                    "MEM-REVIEW-CONT 用户习惯用图像理解连续性。",
                    concept_id=103,
                ),
            ),
            memory_status="SUCCESS",
            revisions={
                103: (
                    rev(
                        710,
                        103,
                        1,
                        summary="早期版本",
                        misconceptions=(
                            misc("TRAJ-REVIEW-CONT 认为连续性就是极限存在"),
                        ),
                        status=UnderstandingStatus.MISCONCEPTION,
                        trigger_evidence_id=1031,
                        created_at=T0,
                    ),
                    rev(
                        711,
                        103,
                        2,
                        summary="已纠正",
                        status=UnderstandingStatus.PARTIAL,
                        trigger_evidence_id=1032,
                        created_at=T3,
                    ),
                )
            },
            revision_status="SUCCESS",
            evidence_details=(
                e(
                    1031,
                    103,
                    "EVID-REVIEW-CONT 用户把极限存在当作连续。",
                    correctness=EvidenceCorrectness.INCORRECT,
                    created_at=T0,
                ),
                e(
                    1032,
                    103,
                    "EVID-REVIEW-CONT-2 用户后来补充了函数值等于极限值。",
                    correctness=EvidenceCorrectness.CORRECT,
                    created_at=T3,
                ),
            ),
            evidences=(),
            evidence_status="EMPTY",
        ),
        expected=Expected(
            intent="LEARNING_REVIEW",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "REQUIRED",
                "evidence_related": "OPTIONAL",
                "revision": "REQUIRED",
                "knowledge": "OPTIONAL",
            },
            injection={
                "understanding:503": "SELECTED",
                "memory:402": "SELECTED",
                "revision:710": "SELECTED",
            },
            markers={
                "understanding:503": "UND-REVIEW-CONT",
                "memory:402": "MEM-REVIEW-CONT",
                "revision:710": "TRAJ-REVIEW-CONT",
            },
            section_contains={
                "trajectory:103": (
                    "已纠正，历史",
                    "EVID-REVIEW-CONT",
                ),
            },
            required_text=(
                "## 学习者的当前认知",
                "## 学习轨迹（历史记录，不等于当前状态）",
                "当前状态以「学习者的当前认知」区块为准",
            ),
            forbidden_text=("过去没有误区", "没有历史记录"),
            personalization_sources=(
                "understanding:503",
                "memory:402",
                "revision:710",
            ),
        ),
    ),
)


# =====================================================================
# 5. 评估掌握程度（META_QUESTION）
# =====================================================================


CASES += (
    Case(
        case_id="meta-derivative-mastery",
        category="meta_question",
        description="掌握程度：当前快照 + 正反证据，不读完整 Revision 历史。",
        user_message="我现在的掌握程度怎么样？",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(_EXPLAIN_DERIV_U,),
            understanding_status="SUCCESS",
            memories=(_EXPLAIN_DERIV_M,),
            memory_status="SUCCESS",
            evidences=(
                e(
                    1023,
                    102,
                    "EVID-META-DERIV 用户能解释导数是瞬时变化率。",
                    correctness=EvidenceCorrectness.CORRECT,
                    created_at=T2,
                ),
                e(
                    1024,
                    102,
                    "EVID-META-DERIV-2 用户在复合函数求导时漏掉内层导数。",
                    correctness=EvidenceCorrectness.INCORRECT,
                    created_at=T3,
                ),
            ),
            evidence_status="SUCCESS",
            knowledge=(),
            knowledge_status="NO_MATCH",
        ),
        expected=Expected(
            intent="META_QUESTION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "REQUIRED",
                "evidence_related": "OPTIONAL",
                "revision": "SKIP",
                "knowledge": "OPTIONAL",
            },
            injection={
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "evidence:1023": "SELECTED",
                "evidence:1024": "SELECTED",
            },
            markers={
                "understanding:500": "UND-DERIV",
                "memory:400": "MEM-DERIV",
                "evidence:1023": "EVID-META-DERIV",
                "evidence:1024": "EVID-META-DERIV-2",
            },
            required_text=(
                "## 学习者的当前认知",
                "## 判定依据",
            ),
            forbidden_text=("没有历史记录", "用户没有相关历史"),
            personalization_sources=(
                "understanding:500",
                "memory:400",
                "evidence:1023",
                "evidence:1024",
            ),
        ),
    ),
    Case(
        case_id="meta-tlb-weakness",
        category="meta_question",
        description="薄弱点：当前误区 + 答错证据，应被显式呈现。",
        user_message="我在 TLB 上哪里薄弱？",
        concept_ids=(202,),
        retrieved=Retrieved(
            understandings=(
                u(
                    502,
                    202,
                    "TLB（快表）",
                    summary="UND-META-TLB 用户知道 TLB 是页表项缓存，但混淆 TLB Miss 与 Page Fault。",
                    known_parts=("TLB 是页表项缓存",),
                    misconceptions=(
                        misc("MISC-TLB 把 TLB Miss 当成 Page Fault"),
                    ),
                    status=UnderstandingStatus.MISCONCEPTION,
                    updated_at=T3,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    401,
                    "MEM-META-TLB 用户希望先定位薄弱点再讲解。",
                    concept_id=202,
                ),
            ),
            memory_status="SUCCESS",
            evidences=(
                e(
                    2021,
                    202,
                    "EVID-META-TLB 用户把 TLB Miss 说成缺页。",
                    correctness=EvidenceCorrectness.INCORRECT,
                    created_at=T2,
                ),
                e(
                    2022,
                    202,
                    "EVID-META-TLB-2 用户能解释 TLB 命中。",
                    correctness=EvidenceCorrectness.CORRECT,
                    created_at=T3,
                ),
            ),
            evidence_status="SUCCESS",
            knowledge=(),
            knowledge_status="NO_MATCH",
        ),
        expected=Expected(
            intent="META_QUESTION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "REQUIRED",
                "evidence_related": "OPTIONAL",
                "revision": "SKIP",
                "knowledge": "OPTIONAL",
            },
            injection={
                "understanding:502": "SELECTED",
                "memory:401": "SELECTED",
                "evidence:2021": "SELECTED",
                "evidence:2022": "SELECTED",
            },
            markers={
                "understanding:502": "UND-META-TLB",
                "memory:401": "MEM-META-TLB",
                "evidence:2021": "EVID-META-TLB",
                "evidence:2022": "EVID-META-TLB-2",
            },
            required_text=(
                "## 学习者的当前认知",
                "## 判定依据",
                "MISC-TLB",
            ),
            personalization_sources=(
                "understanding:502",
                "memory:401",
                "evidence:2021",
                "evidence:2022",
            ),
        ),
    ),
)


# =====================================================================
# 6. 询问教材 / 文档内容（KNOWLEDGE_QUERY）
# =====================================================================


CASES += (
    Case(
        case_id="knowledge-query-derivative-doc",
        category="knowledge_query",
        description="文档问题：Knowledge REQUIRED，认知 / 记忆只是 OPTIONAL 辅助。",
        user_message="这份资料里怎么定义导数？",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(_EXPLAIN_DERIV_U,),
            understanding_status="SUCCESS",
            memories=(_EXPLAIN_DERIV_M,),
            memory_status="SUCCESS",
            knowledge=(_EXPLAIN_DERIV_K,),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="KNOWLEDGE_QUERY",
            retrieval={
                "understanding": "OPTIONAL",
                "memory": "OPTIONAL",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "chunk:700": "SELECTED",
            },
            markers={
                "understanding:500": "UND-DERIV",
                "memory:400": "MEM-DERIV",
                "chunk:700": "KNW-DERIV",
            },
            required_text=("## 参考资料", "## 学习者的当前认知"),
            personalization_sources=("understanding:500", "memory:400"),
        ),
    ),
    Case(
        case_id="knowledge-query-pagetable-textbook",
        category="knowledge_query",
        description="教材原文：多个文档片段注入，来源标注必须可核对。",
        user_message="教材第 3 章讲页表的部分原文是什么？",
        concept_ids=(201,),
        retrieved=Retrieved(
            understandings=(
                u(
                    504,
                    201,
                    "页表",
                    summary="UND-KQ-PT 用户知道页表负责虚拟地址到物理地址的映射。",
                    known_parts=("页表是地址映射结构",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memory_status="EMPTY",
            knowledge=(
                k(
                    702,
                    "KNW-KQ-PT 页表项包含物理页框号与权限位。",
                    document_name="操作系统教材.pdf",
                    heading_path=("第3章 内存管理", "页表"),
                    page_number=42,
                    score=0.95,
                ),
                k(
                    703,
                    "KNW-KQ-PT-2 多级页表用于减少页表占用。",
                    document_name="操作系统教材.pdf",
                    heading_path=("第3章 内存管理", "多级页表"),
                    page_number=43,
                    score=0.8,
                ),
            ),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="KNOWLEDGE_QUERY",
            retrieval={
                "understanding": "OPTIONAL",
                "memory": "OPTIONAL",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:504": "SELECTED",
                "chunk:702": "SELECTED",
                "chunk:703": "SELECTED",
            },
            markers={
                "understanding:504": "UND-KQ-PT",
                "chunk:702": "KNW-KQ-PT",
                "chunk:703": "KNW-KQ-PT-2",
            },
            required_text=(
                "## 参考资料",
                "第3章 内存管理",
                "第 42 页",
            ),
            forbidden_text=("资料中不存在",),
            personalization_sources=("understanding:504",),
        ),
    ),
)


# =====================================================================
# 7. 主题切换（验证不污染）
# =====================================================================


CASES += (
    Case(
        case_id="topic-switch-derivative-to-tcp",
        category="topic_switch",
        description="SessionResolver 把新话题挂到旧 Session：旧 goal/focus 必须被剥离。",
        user_message="现在开始学 TCP 的三次握手",
        concept_ids=(301,),
        session=SessionContext(
            id=30,
            title="导数与极限",
            goal="SESSION-STALE-DERIV 掌握导数",
            current_focus="SESSION-STALE-FOCUS 链式法则",
        ),
        retrieved=Retrieved(
            understandings=(
                u(
                    530,
                    301,
                    "TCP",
                    summary="UND-TCP 用户知道 TCP 是可靠传输协议。",
                    known_parts=("TCP 是可靠传输",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(430, "MEM-TCP 用户偏好用时序图理解协议。", concept_id=301),
            ),
            memory_status="SUCCESS",
            evidences=(
                e(3011, 301, "EVID-TCP 用户描述过 TCP 的可靠传输。", correctness=EvidenceCorrectness.CORRECT),
            ),
            evidence_status="SUCCESS",
            knowledge=(
                k(
                    730,
                    "KNW-TCP 三次握手用于建立连接。",
                    document_name="网络讲义.pdf",
                ),
            ),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            session_inject_goal=False,
            session_inject_focus=False,
            session_concept_match=False,
            session_reason="session_concept_mismatch",
            injection={
                "understanding:530": "SELECTED",
                "memory:430": "SELECTED",
                "chunk:730": "SELECTED",
            },
            must_not_inject=("session:30",),
            markers={
                "understanding:530": "UND-TCP",
                "memory:430": "MEM-TCP",
                "chunk:730": "KNW-TCP",
            },
            required_text=("## 学习者的当前认知", "## 参考资料"),
            forbidden_text=(
                "SESSION-STALE-DERIV",
                "SESSION-STALE-FOCUS",
                "UND-DERIV",
                "MEM-DERIV",
                "EVID-OLD-DERIV",
            ),
            personalization_sources=("understanding:530", "memory:430"),
        ),
    ),
    Case(
        case_id="topic-switch-tcp-to-pagetable",
        category="topic_switch",
        description="旧 TCP Session 不匹配新页表话题：旧目标不得进入 Prompt。",
        user_message="换个话题，讲讲操作系统的页表",
        concept_ids=(201,),
        session=SessionContext(
            id=31,
            title="TCP 拥塞控制",
            goal="SESSION-STALE-TCP 掌握拥塞控制",
            current_focus="SESSION-STALE-TCP-FOCUS 慢启动",
        ),
        retrieved=Retrieved(
            understandings=(
                u(
                    504,
                    201,
                    "页表",
                    summary="UND-SWITCH-PT 用户知道页表是地址映射结构。",
                    known_parts=("页表是地址映射结构",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(431, "MEM-SWITCH-PT 用户偏好先看页表示意图。", concept_id=201),
            ),
            memory_status="SUCCESS",
            evidences=(
                e(2011, 201, "EVID-PT 用户画过页表层级结构。", correctness=EvidenceCorrectness.CORRECT),
            ),
            evidence_status="SUCCESS",
            knowledge=(
                k(
                    731,
                    "KNW-SWITCH-PT 页表把虚拟页映射到物理页框。",
                    document_name="操作系统讲义.pdf",
                ),
            ),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            session_inject_goal=False,
            session_inject_focus=False,
            session_concept_match=False,
            session_reason="session_concept_mismatch",
            injection={
                "understanding:504": "SELECTED",
                "memory:431": "SELECTED",
                "chunk:731": "SELECTED",
            },
            must_not_inject=("session:31",),
            markers={
                "understanding:504": "UND-SWITCH-PT",
                "memory:431": "MEM-SWITCH-PT",
                "chunk:731": "KNW-SWITCH-PT",
            },
            required_text=("## 学习者的当前认知", "## 参考资料"),
            forbidden_text=(
                "SESSION-STALE-TCP",
                "SESSION-STALE-TCP-FOCUS",
                "UND-OLD-TCP",
                "MEM-OLD-TCP",
                "EVID-OLD-TCP",
            ),
            personalization_sources=("understanding:504", "memory:431"),
        ),
    ),
)


# =====================================================================
# 8. 历史 Evidence 与当前 Understanding 不一致（保留双方）
# =====================================================================


CASES += (
    Case(
        case_id="conflict-current-understanding-vs-old-evidence",
        category="history_current_conflict",
        description="当前快照与旧 Evidence / 旧 Memory 冲突：两边都保留，以当前快照为准。",
        user_message="为什么你认为我现在理解导数了？依据是什么",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(
                u(
                    500,
                    102,
                    "导数",
                    summary="CURRENT-DERIV 用户现在理解链式法则，能正确求复合函数导数。",
                    known_parts=("链式法则",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T4,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    400,
                    "OLD-MEMORY-DERIV 用户过去认为链式法则只需乘以外层导数。",
                    concept_id=102,
                    created_at=T0,
                ),
            ),
            memory_status="SUCCESS",
            evidences=(
                e(
                    1021,
                    102,
                    "OLD-EVIDENCE-DERIV 用户把内外函数顺序弄反。",
                    correctness=EvidenceCorrectness.INCORRECT,
                    created_at=T0,
                ),
            ),
            evidence_status="SUCCESS",
            knowledge=(_EXPLAIN_DERIV_K,),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "REQUIRED",
                "evidence_related": "OPTIONAL",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:500": "SELECTED",
                "memory:400": "SELECTED",
                "evidence:1021": "SELECTED",
                "chunk:700": "SELECTED",
            },
            markers={
                "understanding:500": "CURRENT-DERIV",
                "memory:400": "OLD-MEMORY-DERIV",
                "evidence:1021": "OLD-EVIDENCE-DERIV",
                "chunk:700": "KNW-DERIV",
            },
            section_contains={
                "understanding:102": (
                    "CURRENT-DERIV",
                    "当前快照更新于 2026-02-01 09:00",
                ),
                "memory:400": (
                    "OLD-MEMORY-DERIV",
                    "可能与当前认知冲突",
                    "以当前快照为准",
                ),
                "evidence:1021": ("OLD-EVIDENCE-DERIV",),
            },
            section_excludes={
                "understanding:102": ("OLD-EVIDENCE-DERIV",),
            },
            required_text=(
                "## 学习者的当前认知",
                "## 这个用户的长期记忆",
                "## 判定依据",
                "记录于 2026-01-01 09:00",
            ),
            forbidden_text=("你现在仍然把内外函数顺序弄反", "没有历史记录"),
            personalization_sources=(
                "understanding:500",
                "memory:400",
                "evidence:1021",
            ),
        ),
    ),
    Case(
        case_id="conflict-corrected-tlb-misconception",
        category="history_current_conflict",
        description="已纠正的历史误区：只能在轨迹中说「过去曾经」，不能写成当前仍在。",
        user_message="复盘一下我之前把 TLB Miss 当成 Page Fault 的错误",
        concept_ids=(202,),
        retrieved=Retrieved(
            understandings=(
                u(
                    502,
                    202,
                    "TLB（快表）",
                    summary="CURRENT-TLB 用户现在能区分 TLB Miss 与 Page Fault。",
                    known_parts=("TLB Miss 与 Page Fault 不同",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T5,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    401,
                    "MEM-REVIEW-TLB 用户过去把 TLB Miss 当成缺页。",
                    concept_id=202,
                    created_at=T0,
                ),
            ),
            memory_status="SUCCESS",
            revisions={
                202: (
                    rev(
                        720,
                        202,
                        1,
                        summary="早期版本",
                        misconceptions=(
                            misc("TRAJ-TLB 把 TLB Miss 当成 Page Fault"),
                        ),
                        status=UnderstandingStatus.MISCONCEPTION,
                        trigger_evidence_id=2021,
                        created_at=T0,
                    ),
                    rev(
                        721,
                        202,
                        2,
                        summary="已纠正",
                        status=UnderstandingStatus.PARTIAL,
                        trigger_evidence_id=2022,
                        created_at=T5,
                    ),
                )
            },
            revision_status="SUCCESS",
            evidence_details=(
                e(
                    2021,
                    202,
                    "EVID-TLB-MISS 用户说 TLB Miss 就是缺页。",
                    correctness=EvidenceCorrectness.INCORRECT,
                    created_at=T0,
                ),
                e(
                    2022,
                    202,
                    "EVID-TLB-MISS-2 用户后来区分了二者。",
                    correctness=EvidenceCorrectness.CORRECT,
                    created_at=T5,
                ),
            ),
            evidences=(),
            evidence_status="EMPTY",
        ),
        expected=Expected(
            intent="LEARNING_REVIEW",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "REQUIRED",
                "evidence_related": "OPTIONAL",
                "revision": "REQUIRED",
                "knowledge": "OPTIONAL",
            },
            injection={
                "understanding:502": "SELECTED",
                "memory:401": "SELECTED",
                "revision:720": "SELECTED",
            },
            markers={
                "understanding:502": "CURRENT-TLB",
                "memory:401": "MEM-REVIEW-TLB",
                "revision:720": "TRAJ-TLB",
            },
            section_contains={
                "trajectory:202": (
                    "已纠正，历史",
                    "EVID-TLB-MISS",
                ),
                "understanding:202": ("CURRENT-TLB",),
            },
            section_excludes={
                "understanding:202": ("TRAJ-TLB",),
                "trajectory:202": ("当前仍存在的误区",),
            },
            required_text=(
                "## 学习者的当前认知",
                "## 学习轨迹（历史记录，不等于当前状态）",
                "当前状态以「学习者的当前认知」区块为准",
            ),
            forbidden_text=(
                "你现在仍然把 TLB Miss 当成 Page Fault",
                "过去没有误区",
            ),
            personalization_sources=(
                "understanding:502",
                "memory:401",
                "revision:720",
            ),
        ),
    ),
)


# =====================================================================
# 9. 检索 DEGRADED / 部分数据不可访问
# =====================================================================


CASES += (
    Case(
        case_id="degraded-understanding-memory-knowledge",
        category="degraded_retrieval",
        description="U / M / K 都 DEGRADED，但有部分数据：必须显示降级，不得说成没有记录。",
        user_message="TLB 是什么？",
        concept_ids=(202,),
        retrieved=Retrieved(
            understandings=(
                u(
                    502,
                    202,
                    "TLB（快表）",
                    summary="DEG-UND-TLB 用户知道 TLB 是页表项缓存（部分结果）。",
                    known_parts=("TLB 是页表项缓存",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="DEGRADED",
            understanding_warning=(
                "学习认知检索失败或不完整（conceptIds=[202]），"
                "无法确认这些知识点是否已有完整认知记录。"
            ),
            memories=(
                m(
                    401,
                    "DEG-MEM-TLB 用户偏好地址翻译流程图（部分结果）。",
                    concept_id=202,
                ),
            ),
            memory_status="DEGRADED",
            memory_detail="UNAVAILABLE",
            memory_warning="记忆检索不可用，无法确认是否有相关记忆。",
            knowledge=(),
            knowledge_status="DEGRADED",
            knowledge_warning="知识检索不可用（Embedding 服务失败）。",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:502": "SELECTED",
                "memory:401": "SELECTED",
                "understanding-status:DEGRADED": "SELECTED",
                "knowledge-status:DEGRADED": "SELECTED",
            },
            markers={
                "understanding:502": "DEG-UND-TLB",
                "memory:401": "DEG-MEM-TLB",
            },
            required_text=(
                "学习认知检索：DEGRADED",
                "长期记忆检索：DEGRADED",
                "本轮知识检索不可用（Embedding / 向量检索失败）",
                "无法判断上传文档是否包含答案",
            ),
            forbidden_text=(
                "没有历史记录",
                "用户没有相关记忆",
                "资料中不存在",
                "用户没有相关历史",
            ),
            degraded_sources=("understanding", "memory", "knowledge"),
            personalization_sources=("understanding:502", "memory:401"),
        ),
    ),
    Case(
        case_id="degraded-revision-review",
        category="degraded_retrieval",
        description="复盘场景 Revision DEGRADED：不得说「过去没有误区」，但要如实说明无法确认。",
        user_message="我之前的导数学习错在哪里？",
        concept_ids=(102,),
        retrieved=Retrieved(
            understandings=(
                u(
                    500,
                    102,
                    "导数",
                    summary="UND-DEG-REV 用户当前理解导数是瞬时变化率。",
                    known_parts=("导数是瞬时变化率",),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memory_status="EMPTY",
            knowledge=(),
            knowledge_status="NO_MATCH",
            revisions={},
            revision_status="DEGRADED",
            revision_warning=(
                "历史轨迹检索失败或不完整（conceptIds=[102]），"
                "无法确认用户过去是否存在误区或变化。"
            ),
        ),
        expected=Expected(
            intent="LEARNING_REVIEW",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "REQUIRED",
                "knowledge": "OPTIONAL",
            },
            injection={
                "understanding:500": "SELECTED",
            },
            markers={
                "understanding:500": "UND-DEG-REV",
            },
            required_text=(
                "历史轨迹检索：DEGRADED",
                "不能据此判断用户过去没有误区",
                "本轮知识检索没有找到可用的文档片段",
            ),
            forbidden_text=(
                "过去没有误区",
                "没有历史记录",
                "用户没有相关历史",
                "资料中不存在",
            ),
            degraded_sources=("revision",),
            personalization_sources=("understanding:500",),
        ),
    ),
)


# =====================================================================
# 10. 预算压力
# =====================================================================


_LONG_ORBIT = (
    "KNW-LONG-ORBIT ORBIT-4721 的调度规范与重试窗口说明。"
    "当前调度器使用 17 秒的重试窗口，该值适用于所有新部署。"
) * 80


CASES += (
    Case(
        case_id="budget-knowledge-partial",
        category="budget_pressure",
        description="知识检索成功但长片段被类型预算裁掉：必须显示 SUCCESS_PARTIAL。",
        user_message="ORBIT-4721 新部署在失败后等待多久再重试？",
        concept_ids=(),
        max_context_tokens=400,
        output_reserve_ratio=0.0,
        retrieved=Retrieved(
            knowledge=(
                k(
                    701,
                    _LONG_ORBIT,
                    document_name="orbit_current.md",
                    score=0.95,
                ),
                k(
                    702,
                    "KNW-SHORT-ORBIT 新部署失败后等待 17 秒再重试。",
                    document_name="orbit_retry.md",
                    score=0.8,
                ),
            ),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="KNOWLEDGE_QUERY",
            retrieval={
                "understanding": "SKIP",
                "memory": "SKIP",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "chunk:701": "OMITTED",
                "chunk:702": "SELECTED",
                "knowledge-status:SUCCESS_PARTIAL": "SELECTED",
            },
            injection_reasons={"chunk:701": "type_budget"},
            markers={
                "chunk:701": "KNW-LONG-ORBIT",
                "chunk:702": "KNW-SHORT-ORBIT",
            },
            required_text=(
                "SUCCESS_PARTIAL",
                "只能依据「参考资料」中实际出现的片段",
            ),
            forbidden_text=("没有检索到资料", "资料中没有答案"),
            knowledge_state="SUCCESS_PARTIAL",
        ),
    ),
    Case(
        case_id="budget-tier-truncation",
        category="budget_pressure",
        description="总预算充足但类型额度不足：Understanding / Memory 降档为 TRUNCATED，内容不丢来源。",
        user_message="极限的直观理解是什么？",
        concept_ids=(101,),
        max_context_tokens=900,
        output_reserve_ratio=0.0,
        retrieved=Retrieved(
            understandings=(
                u(
                    510,
                    101,
                    "极限",
                    summary="UND-TIER-LIMIT 用户理解极限是无限趋近。",
                    known_parts=tuple(
                        f"已知第{i}点：极限描述函数在自变量趋近某值时的趋势"
                        for i in range(8)
                    ),
                    uncertain_parts=tuple(
                        f"不确定第{i}点：还没有把 ε-δ 语言与几何直觉对应起来"
                        for i in range(8)
                    ),
                    latest_user_explanation=(
                        "用户用自己的话说：极限就是越走越近，但还没有说清楚"
                        "靠近到什么程度、是否必须取到、以及左右极限不同时怎么办。"
                    ),
                    status=UnderstandingStatus.PARTIAL,
                    updated_at=T2,
                ),
            ),
            understanding_status="SUCCESS",
            memories=(
                m(
                    410,
                    "MEM-TIER-LIMIT 用户偏好先用图像和逼近过程理解极限，"
                    "再补充 ε-δ 定义；看到纯符号推导时容易失去耐心。"
                    "讲解时应先画出函数图像，标出自变量从左右两侧趋近某点的过程，"
                    "再说明函数值如何随之变化；只有图像建立直觉之后，才引入"
                    "形式化定义、量词顺序和不等式的含义，并逐步过渡到例题。"
                    "如果一次讲解内容较多，希望先给出一个可复述的总结，再展开细节；"
                    "遇到左右极限不相等、函数在该点无定义或振荡不收敛等情况时，"
                    "要分别举例说明，不要只用一句“趋近”带过。",
                    concept_id=101,
                ),
            ),
            memory_status="SUCCESS",
            knowledge=(
                k(
                    750,
                    "KNW-TIER-LIMIT 极限描述自变量趋近某值时函数值的趋势。",
                    document_name="极限讲义.pdf",
                ),
            ),
            knowledge_status="SUCCESS",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "understanding:510": "TRUNCATED",
                "memory:410": "TRUNCATED",
                "chunk:750": "SELECTED",
            },
            injection_reasons={
                "understanding:510": "tier:one_line",
                "memory:410": "tier:structured",
            },
            markers={
                "understanding:510": "UND-TIER-LIMIT",
                "memory:410": "MEM-TIER-LIMIT",
                "chunk:750": "KNW-TIER-LIMIT",
            },
            required_text=(
                "## 学习者的当前认知",
                "## 这个用户的长期记忆",
                "## 参考资料",
            ),
            forbidden_text=("没有历史记录", "用户没有相关记忆"),
            personalization_sources=("understanding:510", "memory:410"),
            knowledge_state="SUCCESS",
        ),
    ),
    Case(
        case_id="budget-protected-overflow",
        category="budget_pressure",
        description="极小预算下 protected 状态说明超出总额：Manifest 必须如实记录 used > total。",
        user_message="TLB 是什么？",
        concept_ids=(202,),
        session=SessionContext(
            id=40,
            title="TLB 快表",
            goal="SESSION-GOAL-OVERFLOW 理解 TLB 命中流程",
            current_focus="SESSION-FOCUS-OVERFLOW TLB Miss",
        ),
        max_context_tokens=60,
        output_reserve_ratio=0.0,
        retrieved=Retrieved(
            understanding_status="DEGRADED",
            understanding_warning="学习认知检索失败，无法确认是否已有认知记录。",
            memory_status="DEGRADED",
            memory_detail="UNAVAILABLE",
            memory_warning="记忆检索不可用，无法确认是否有相关记忆。",
            knowledge=(),
            knowledge_status="DEGRADED",
            knowledge_warning="知识检索不可用（Embedding 服务失败）。",
        ),
        expected=Expected(
            intent="LEARNING_EXPLANATION",
            retrieval={
                "understanding": "REQUIRED",
                "memory": "REQUIRED",
                "evidence": "SKIP",
                "evidence_related": "SKIP",
                "revision": "SKIP",
                "knowledge": "REQUIRED",
            },
            injection={
                "session:40": "SELECTED",
                "understanding-status:DEGRADED": "SELECTED",
                "knowledge-status:DEGRADED": "SELECTED",
            },
            required_text=(
                "学习认知检索：DEGRADED",
                "本轮知识检索不可用（Embedding / 向量检索失败）",
                "不能据此判断",
            ),
            forbidden_text=("没有历史记录", "用户没有相关记忆"),
            degraded_sources=("understanding", "memory", "knowledge"),
            allow_known_overflow=True,
        ),
    ),
)


# =====================================================================
# 校验辅助
# =====================================================================


CATEGORY_ORDER: tuple[str, ...] = (
    "learning_explanation",
    "continue_task_same_session",
    "cross_session_learning",
    "learning_review",
    "meta_question",
    "knowledge_query",
    "topic_switch",
    "history_current_conflict",
    "degraded_retrieval",
    "budget_pressure",
)


def cases_by_category() -> dict[str, tuple[Case, ...]]:
    grouped: dict[str, list[Case]] = {category: [] for category in CATEGORY_ORDER}
    for case in CASES:
        grouped.setdefault(case.category, []).append(case)
    return {category: tuple(items) for category, items in grouped.items()}
