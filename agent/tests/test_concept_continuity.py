"""Concept 连续性：跨 Conversation / Session 的隔离与回读契约。

这些测试不连 MySQL、不连后端，也不让 Runtime 装配 Fixture：
它们用一个**只按 user + space + concept_id 存取**的假检索源，模拟后端
Understanding / Evidence / Memory 的真实锚点，然后让同一个 AgentRuntime
连续处理来自不同 Conversation、不同 Session 的请求。

要钉死的是四件事：

1. 同一个 concept_id 的数据，在另一条 Conversation 里仍然读得到；
2. 切到不相关的概念时，旧概念的 Understanding / Evidence 不会被注入；
3. 回到旧概念时，拿到的是**当前** Understanding 与最近的 Evidence；
4. Session 只是活动上下文，既不拥有也不覆盖 concept 层状态。

假检索源刻意不接收 conversation_id / session：这不是偷懒，而是照抄
`Retriever` 协议与后端表的真实形状 —— 概念层数据本来就没有这两个维度。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.llm import build_llm_adapter
from app.retriever.base import (
    EvidenceRetrievalResult,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
    RelatedConceptResult,
    RelatedConceptStatus,
)
from app.retriever.concept_detector import detect_concepts
from app.runtime import AgentRuntime
from app.schemas import (
    Concept,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceType,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    Misconception,
    SessionContext,
    Understanding,
    UnderstandingStatus,
)
from tests.helpers import make_request

TLB_ID = 10
REDIS_ID = 20
SPACE_ID = 1

TLB = Concept(id=TLB_ID, canonical_name="TLB", display_name="TLB（快表）")
REDIS = Concept(id=REDIS_ID, canonical_name="Redis", display_name="Redis")

T0 = datetime(2026, 1, 1, 9, 0)
T1 = datetime(2026, 1, 2, 9, 0)


class ConceptStoreRetriever:
    """模拟后端的 concept 层存储。

    所有读写都只接受 `(user_id, space_id, concept_ids)`：
    Conversation 只决定「谁把这条消息送到 Agent」，Session 只决定
    「当前活动目标是什么」，两者都不进入概念层数据的键。
    """

    name = "concept-store"

    def __init__(self) -> None:
        self.concepts = [TLB, REDIS]
        self.understandings: dict[int, Understanding] = {}
        self.evidences: list[EvidenceItem] = []
        self.memories = [
            MemoryItem(
                id=400,
                scope_type=MemoryScopeType.CONCEPT,
                learning_space_id=SPACE_ID,
                concept_id=TLB_ID,
                content="TLB-CONCEPT-MEMORY-MARKER 用户把 TLB 当成页表本身",
                status=MemoryStatus.ACTIVE,
            ),
            MemoryItem(
                id=401,
                scope_type=MemoryScopeType.SPACE,
                learning_space_id=SPACE_ID,
                content="SPACE-MEMORY-MARKER 用户偏好从硬件实际发生了什么入手",
                status=MemoryStatus.ACTIVE,
            ),
            MemoryItem(
                id=402,
                scope_type=MemoryScopeType.GLOBAL,
                content="GLOBAL-MEMORY-MARKER 用户偏好先给结论再展开",
                status=MemoryStatus.ACTIVE,
            ),
        ]
        # 每次检索都记下调用参数，用来证明确实只按 concept_id 取数。
        self.calls: list[tuple] = []

    # ---------- 模拟后端分析写入（锚点只有 user + concept） ----------

    def add_evidence(self, concept_id: int, evidence_id: int, observation: str,
                     created_at: datetime) -> None:
        self.evidences.append(
            EvidenceItem(
                id=evidence_id,
                concept_id=concept_id,
                evidence_type=EvidenceType.OBSERVATION,
                observation=observation,
                correctness=EvidenceCorrectness.UNKNOWN,
                created_at=created_at,
            )
        )

    def set_understanding(
        self,
        concept_id: int,
        *,
        summary: str,
        known_parts: list[str] | None = None,
        misconceptions: list[str] | None = None,
        status: UnderstandingStatus = UnderstandingStatus.PARTIAL,
    ) -> None:
        concept = next(item for item in self.concepts if item.id == concept_id)
        self.understandings[concept_id] = Understanding(
            id=500 + concept_id,
            concept_id=concept_id,
            concept_name=concept.label,
            summary=summary,
            known_parts=list(known_parts or []),
            misconceptions=[Misconception(text=text) for text in (misconceptions or [])],
            status=status,
            updated_at=T1,
        )

    # ---------- Retriever 协议 ----------

    async def list_concepts(self, space_id: int) -> list[Concept]:
        return list(self.concepts)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        self.calls.append(("understandings", user_id, space_id, tuple(concept_ids)))
        return [
            self.understandings[concept_id]
            for concept_id in concept_ids
            if concept_id in self.understandings
        ]

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        self.calls.append(("memories", user_id, space_id, tuple(concept_ids)))
        wanted = set(concept_ids)
        return [
            item
            for item in self.memories
            if item.status is MemoryStatus.ACTIVE
            and (
                item.scope_type is MemoryScopeType.GLOBAL
                or (
                    item.scope_type is MemoryScopeType.SPACE
                    and item.learning_space_id == space_id
                )
                or (
                    item.scope_type is MemoryScopeType.CONCEPT
                    and item.learning_space_id == space_id
                    and item.concept_id in wanted
                )
            )
        ]

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

    async def get_evidences(
        self, user_id: int, space_id: int, concept_ids: list[int], limit: int
    ) -> EvidenceRetrievalResult:
        self.calls.append(("evidences", user_id, space_id, tuple(concept_ids)))
        if not concept_ids or limit <= 0:
            return EvidenceRetrievalResult()
        wanted = set(concept_ids)
        items = [item for item in self.evidences if item.concept_id in wanted]
        items.sort(key=lambda item: (item.created_at or T0, item.id))
        return EvidenceRetrievalResult.from_items(items[-limit:])

    async def get_related_concepts(self, *args, **kwargs) -> RelatedConceptResult:
        # 没有关系数据：EMPTY 是「本来就没有」，不是查询失败。
        return RelatedConceptResult(status=RelatedConceptStatus.EMPTY)

    async def get_revisions(self, user_id: int, concept_ids: list[int]) -> dict:
        return {concept_id: [] for concept_id in concept_ids}

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ) -> list[EvidenceItem]:
        wanted = set(concept_ids)
        ids = set(evidence_ids)
        return [
            item
            for item in self.evidences
            if item.id in ids and item.concept_id in wanted
        ]

    async def aclose(self) -> None:
        return None


@pytest.fixture
def runtime(settings) -> AgentRuntime:
    # Resolver 会引入模型判断，不是这组测试要验的对象；关掉它，
    # 让概念识别完全走确定性的规则路径，结论可复现。
    isolated = settings.model_copy(update={"concept_resolver_enabled": False})
    return AgentRuntime(isolated, build_llm_adapter(isolated))


async def prepare(
    runtime: AgentRuntime,
    store: ConceptStoreRetriever,
    message: str,
    *,
    conversation_id: int,
    session: SessionContext | None = None,
    history: list[tuple[str, str]] | None = None,
):
    request = make_request(
        message,
        conversation_id=conversation_id,
        session=session,
        history=history,
    )
    return await runtime.prepare(request, retriever=store)


def system_prompt(prepared) -> str:
    return prepared.context.messages[0].content


# ===================== 概念识别：TLB Miss → TLB =====================


def test_tlb_miss_is_the_same_concept_when_space_defines_only_tlb():
    """Space 里只有 TLB 时，「TLB Miss」必须落到同一个 concept_id。

    这不是靠别名表把两个概念合并：`TLB Miss` 里的 `TLB` 是独立的 ASCII 词，
    识别规则按词边界命中 canonical_name。大小写不敏感。
    """
    for text in ("TLB Miss 没命中时会发生什么？", "tlb miss 没命中时会发生什么？"):
        detected = detect_concepts(text, [], [TLB, REDIS])
        assert [concept.id for concept, _, _ in detected] == [TLB_ID]


def test_ascii_concept_name_requires_word_boundary():
    """TLBSize 不是 TLB。宁可漏召回，也不能把不相关的话写成 TLB 证据。"""
    assert detect_concepts("TLBSize 一般怎么调？", [], [TLB, REDIS]) == []


# ===================== 场景 1 → 2：跨 Conversation 读同一份概念状态 =====================


async def test_conversation_b_reads_evidence_and_understanding_written_by_a(runtime):
    store = ConceptStoreRetriever()

    # Conversation A：第一次谈 TLB。此时后端还没有快照。
    a = await prepare(runtime, store, "TLB Miss 是什么？", conversation_id=101)
    assert a.state.concept_ids == [TLB_ID]
    assert a.state.understandings == []

    # 模拟 A 的分析写入：Evidence 与 Understanding 都只锚在 user + concept 上，
    # 不写 conversation_id / session_id。
    store.add_evidence(
        TLB_ID,
        901,
        "A-回合证据：用户把 TLB Miss 与 Page Fault 混为一谈",
        T0,
    )
    store.set_understanding(
        TLB_ID,
        summary="A-回合后的当前理解：TLB 是页表项的缓存",
        known_parts=["TLB 缓存页表项"],
        misconceptions=["把 TLB Miss 当成 Page Fault"],
    )

    # Conversation B：换了一条对话，也没有带 A 的历史。
    # 仅凭本轮消息里的 TLB 就应命中同一个 concept，并读回 A 写入的状态。
    b = await prepare(
        runtime,
        store,
        "TLB Miss 没命中时为什么会出现？",
        conversation_id=202,
    )

    assert b.state.concept_ids == [TLB_ID]
    prompt = system_prompt(b)
    assert "A-回合后的当前理解" in prompt
    assert "把 TLB Miss 当成 Page Fault" in prompt
    assert [evidence.id for evidence in b.state.evidences] == [901]
    assert "A-回合证据" in prompt

    # 两次请求只有 conversation_id 不同，检索接口收到的始终是同一个 concept_id。
    understanding_calls = [
        call for call in store.calls if call[0] == "understandings"
    ]
    assert [call[3] for call in understanding_calls] == [(TLB_ID,), (TLB_ID,)]


# ===================== 场景 3：切到无关概念时不注入旧概念状态 =====================


async def test_redis_conversation_does_not_receive_tlb_state(runtime):
    store = ConceptStoreRetriever()
    store.add_evidence(TLB_ID, 901, "TLB-EVIDENCE-MARKER 旧话题的证据", T0)
    store.set_understanding(
        TLB_ID,
        summary="TLB-UNDERSTANDING-MARKER 旧话题的当前理解",
    )

    redis = await prepare(
        runtime,
        store,
        "Redis 的缓存淘汰策略是什么？",
        conversation_id=303,
    )

    assert redis.state.concept_ids == [REDIS_ID]
    assert redis.state.understandings == []
    assert redis.state.evidences == []

    prompt = system_prompt(redis)
    for marker in (
        "TLB-UNDERSTANDING-MARKER",
        "TLB-EVIDENCE-MARKER",
        "TLB-CONCEPT-MEMORY-MARKER",
    ):
        assert marker not in prompt

    # SPACE / GLOBAL 记忆按作用域本来就可见 —— 这不是概念状态污染，
    # 而是用户级 / 空间级事实的既定语义。
    assert "SPACE-MEMORY-MARKER" in prompt
    assert "GLOBAL-MEMORY-MARKER" in prompt

    # 只查了 Redis 的 concept_id，没有顺带把 TLB 的 Understanding 拉回来。
    understanding_calls = [
        call for call in store.calls if call[0] == "understandings"
    ]
    assert [call[3] for call in understanding_calls] == [(REDIS_ID,)]


# ===================== 场景 4：回到旧概念，拿当前快照 + 最近证据 =====================


async def test_returning_to_tlb_reads_current_understanding_and_recent_evidence(runtime):
    store = ConceptStoreRetriever()

    # A 先写一条，B 又写了更新的一条：概念层只有「当前快照 + 追加证据」。
    store.add_evidence(TLB_ID, 901, "A-回合证据：把 TLB Miss 当成 Page Fault", T0)
    store.add_evidence(TLB_ID, 902, "B-回合证据：补充 TLB Miss 是 TLB 未命中", T1)
    store.set_understanding(
        TLB_ID,
        summary="B-回合更新后的当前理解：TLB 是页表项缓存",
        known_parts=["TLB 缓存页表项"],
        misconceptions=["把 TLB Miss 当成 Page Fault"],
    )

    # 「为什么」触发按需 Evidence 召回；本轮不是个人复盘，不读 Revision 轨迹。
    d = await prepare(
        runtime,
        store,
        "为什么说我对 TLB Miss 的理解还有问题？",
        conversation_id=404,
    )

    assert d.state.concept_ids == [TLB_ID]
    assert d.state.understanding_status == "SUCCESS"
    assert d.state.evidence_status == "SUCCESS"
    assert d.state.history_review_requested is False

    prompt = system_prompt(d)
    assert "B-回合更新后的当前理解" in prompt
    assert "A-回合证据" in prompt
    assert "B-回合证据" in prompt
    assert {evidence.id for evidence in d.state.evidences} == {901, 902}


async def test_plain_tlb_question_reads_understanding_but_not_raw_evidence(runtime):
    """默认只召回 Understanding / Memory；原始 Evidence 仍按需触发。

    这是既定契约（架构文档第十二节），不是跨对话丢失：
    同一条 TLB 证据在带「为什么 / 依据」的追问里就能读到。
    """
    store = ConceptStoreRetriever()
    store.add_evidence(TLB_ID, 901, "A-回合证据", T0)
    store.set_understanding(TLB_ID, summary="TLB-当前理解")

    plain = await prepare(runtime, store, "TLB 是什么？", conversation_id=405)

    assert plain.state.concept_ids == [TLB_ID]
    assert plain.state.understandings
    assert plain.state.evidences == []
    assert plain.state.evidence_status == "NOT_RETRIEVED"


# ===================== 跨 Session：概念状态不随 Session 变化 =====================


async def test_concept_state_survives_session_switch_without_cross_leak(runtime):
    store = ConceptStoreRetriever()
    store.add_evidence(TLB_ID, 901, "A-回合证据：TLB 是页表项缓存", T0)
    store.set_understanding(TLB_ID, summary="TLB-当前理解：页表项缓存")

    one = await prepare(
        runtime,
        store,
        "为什么 TLB 是页表缓存？",
        conversation_id=501,
        session=SessionContext(
            id=1,
            title="活动一",
            goal="SESSION-ONE-GOAL",
            current_focus="SESSION-ONE-FOCUS",
        ),
    )
    two = await prepare(
        runtime,
        store,
        "为什么 TLB 是页表缓存？",
        conversation_id=502,
        session=SessionContext(
            id=2,
            title="活动二",
            goal="SESSION-TWO-GOAL",
            current_focus="SESSION-TWO-FOCUS",
        ),
    )

    # Session 换了，Concept 层的当前快照与证据完全不变。
    assert one.state.concept_ids == two.state.concept_ids == [TLB_ID]
    assert [item.summary for item in one.state.understandings] == [
        item.summary for item in two.state.understandings
    ] == ["TLB-当前理解：页表项缓存"]
    assert [item.id for item in one.state.evidences] == [
        item.id for item in two.state.evidences
    ] == [901]

    # Session 只贡献自己的活动目标；上一个 Session 的 goal/focus 不会串进来。
    first_prompt = system_prompt(one)
    second_prompt = system_prompt(two)
    assert "SESSION-ONE-GOAL" in first_prompt
    assert "SESSION-ONE-FOCUS" in first_prompt
    assert "SESSION-TWO-GOAL" not in first_prompt
    assert "SESSION-TWO-FOCUS" not in first_prompt
    assert "SESSION-TWO-GOAL" in second_prompt
    assert "SESSION-TWO-FOCUS" in second_prompt
    assert "SESSION-ONE-GOAL" not in second_prompt
    assert "SESSION-ONE-FOCUS" not in second_prompt


# ===================== Conversation 隔离的唯一入口：history =====================


async def test_conversation_history_is_the_only_conversation_scoped_input(runtime):
    """Runtime 不存别的 Conversation 的历史。

    同一句「它没命中的时候呢？」：
    - 当前 Conversation 里带 TLB 历史 → 可以回看并接上 TLB；
    - 当前请求没有历史（另一条 Conversation）→ 不会凭空知道 TLB。
    """
    store = ConceptStoreRetriever()
    store.set_understanding(TLB_ID, summary="TLB-当前理解")

    follow_up = await prepare(
        runtime,
        store,
        "它没命中的时候呢？",
        conversation_id=601,
        history=[("user", "TLB 是什么？"), ("assistant", "TLB 是页表项缓存。")],
    )
    assert follow_up.state.concept_ids == [TLB_ID]

    isolated = await prepare(
        runtime,
        store,
        "它没命中的时候呢？",
        conversation_id=602,
    )
    assert isolated.state.concept_ids == []
    assert isolated.state.understanding_status == "NOT_QUERIED"
