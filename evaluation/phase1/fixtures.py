"""Phase 1 隔离 Fixture 与可注入 Retriever。

为什么不用 ``FixtureRetriever``：现有实现只读一份单空间 JSON，且忽略
``user_id``。跨用户/跨空间隔离的断言必须由一个**按请求身份过滤**的检索源完成，
否则「没有串数据」只是数据里恰好没有。

这里仍然复用 Agent 的真实 Pydantic 契约（Concept / Understanding / Memory /
KnowledgeChunk / EvidenceItem / UnderstandingRevision），没有另造一套模型。
Fixture 只在进程内深拷贝，用例之间互不影响。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.schemas import (
    Concept,
    EvidenceItem,
    KnowledgeChunk,
    MemoryItem,
    Misconception,
    MemoryScopeType,
    MemoryStatus,
    Understanding,
    UnderstandingRevision,
)

# 每个 profile 都是同一份基础数据的「受控变体」，不是另写一份 fixture。
# 这样 reviewer 只需要核对一份事实底稿，profile 只改变被测试的那一个维度。
PROFILES = (
    "alice_current",
    "alice_corrected",
    "alice_conflict_memory",
    "alice_explicit_annotation",
    "alice_evidence_only",
    "alice_empty_history",
    "alice_partial",
    "alice_no_valid_documents",
    "alice_no_match",
    "alice_threshold_filtered",
    "alice_degraded",
    "alice_non_ready",
    "alice_budget",
    "alice_citation",
    "alice_db_space",
    "bob_current",
)

_BUDGET_MEMORY = (
    "这是一条仅用于预算评测的长记忆，内容本身不重要，但会占用大量上下文 token。"
    "它描述用户希望先看地址翻译的完整步骤，再看 TLB 的位置，然后看缺页异常。"
) * 14


@dataclass
class SpaceData:
    user_id: int
    space_id: int
    name: str
    concepts: list[Concept]
    understandings: list[Understanding]
    memories: list[MemoryItem]
    knowledge: list[KnowledgeChunk]
    documents: list[dict]
    evidences: list[EvidenceItem]
    revisions: list[UnderstandingRevision]


@dataclass
class RetrieverCall:
    method: str
    user_id: int | None
    space_id: int | None
    concept_ids: list[int] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "userId": self.user_id,
            "spaceId": self.space_id,
            "conceptIds": list(self.concept_ids),
            "ok": self.ok,
        }


class EvalRetriever:
    """按 userId + spaceId 过滤的只读 Retriever，记录每次调用与越界尝试。"""

    def __init__(
        self,
        data: SpaceData,
        *,
        profile: str,
        knowledge_status: str | None = None,
        knowledge_warning: str | None = None,
        knowledge_candidates: int = 0,
        memory_top_k: int = 5,
        knowledge_top_k: int = 5,
    ) -> None:
        self._data = data
        self._profile = profile
        self._memory_top_k = memory_top_k
        self._knowledge_top_k = knowledge_top_k
        self._knowledge_status = knowledge_status
        self._knowledge_warning = knowledge_warning
        self._knowledge_candidates = knowledge_candidates
        self.calls: list[RetrieverCall] = []
        self.isolation_violations: list[dict] = []
        self.closed = False

    @property
    def name(self) -> str:
        return f"phase1_fixture:{self._data.user_id}:{self._data.space_id}:{self._profile}"

    @property
    def data(self) -> SpaceData:
        return self._data

    # ---------- 检索 ----------

    async def list_concepts(self, space_id: int) -> list[Concept]:
        ok = space_id == self._data.space_id
        self._record("list_concepts", None, space_id, ok=ok)
        if not ok:
            return []
        return list(self._data.concepts)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        ok = self._scope_ok(user_id, space_id)
        self._record("get_understandings", user_id, space_id, concept_ids, ok=ok)
        if not ok or not concept_ids:
            return []
        wanted = set(concept_ids)
        return [item for item in self._data.understandings if item.concept_id in wanted]

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        ok = self._scope_ok(user_id, space_id)
        self._record("get_memories", user_id, space_id, concept_ids, ok=ok)
        if not ok:
            return []
        wanted = set(concept_ids)
        active = [item for item in self._data.memories if item.status is MemoryStatus.ACTIVE]
        related = [
            item
            for item in active
            if item.scope_type is MemoryScopeType.GLOBAL
            or (item.scope_type is MemoryScopeType.SPACE and item.learning_space_id == space_id)
            or (
                item.scope_type is MemoryScopeType.CONCEPT
                and item.learning_space_id == space_id
                and item.concept_id in wanted
            )
        ]
        return related[: self._memory_top_k]

    async def get_knowledge(self, query: KnowledgeQuery) -> KnowledgeResult:
        ok = self._scope_ok(query.user_id, query.space_id)
        self._record("get_knowledge", query.user_id, query.space_id, list(query.concept_ids), ok=ok)
        if not ok:
            return KnowledgeResult(
                status=KnowledgeStatus.DEGRADED,
                warning="检索身份与 fixture 绑定身份不一致，已拒绝返回任何文档。",
            )
        if self._knowledge_status:
            status = KnowledgeStatus(self._knowledge_status)
            if status is not KnowledgeStatus.SUCCESS:
                return KnowledgeResult(
                    chunks=[],
                    status=status,
                    warning=self._knowledge_warning,
                    candidates=self._knowledge_candidates,
                )
        chunks = self._knowledge_chunks(query)
        return KnowledgeResult(
            chunks=chunks,
            status=KnowledgeStatus.SUCCESS if chunks else KnowledgeStatus.NO_MATCH,
        )

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        ok = self._scope_ok(user_id, self._data.space_id)
        self._record("get_evidences", user_id, self._data.space_id, concept_ids, ok=ok)
        if not ok or not concept_ids or limit <= 0:
            return []
        wanted = set(concept_ids)
        items = [item for item in self._data.evidences if item.concept_id in wanted]
        return items[-limit:]

    async def get_revisions(
        self, user_id: int, concept_ids: list[int]
    ) -> dict[int, list[UnderstandingRevision]]:
        ok = self._scope_ok(user_id, self._data.space_id)
        self._record("get_revisions", user_id, self._data.space_id, concept_ids, ok=ok)
        if not ok:
            return {}
        result: dict[int, list[UnderstandingRevision]] = {}
        for concept_id in concept_ids:
            revisions = [item for item in self._data.revisions if item.concept_id == concept_id]
            result[concept_id] = sorted(revisions, key=lambda item: item.revision_no)
        return result

    async def get_evidence_details(
        self, user_id: int, concept_ids: list[int], evidence_ids: list[int]
    ) -> list[EvidenceItem]:
        ok = self._scope_ok(user_id, self._data.space_id)
        self._record("get_evidence_details", user_id, self._data.space_id, concept_ids, ok=ok)
        if not ok:
            return []
        wanted = set(concept_ids)
        by_id = {item.id: item for item in self._data.evidences}
        items = [
            by_id[evidence_id]
            for evidence_id in evidence_ids
            if evidence_id in by_id and by_id[evidence_id].concept_id in wanted
        ]
        items.sort(key=lambda item: item.created_at or datetime.min)
        return items

    async def aclose(self) -> None:
        self.closed = True

    # ---------- 内部 ----------

    def _scope_ok(self, user_id: int, space_id: int) -> bool:
        return user_id == self._data.user_id and space_id == self._data.space_id

    def _record(
        self,
        method: str,
        user_id: int | None,
        space_id: int | None,
        concept_ids: list[int] | None = None,
        *,
        ok: bool,
    ) -> None:
        self.calls.append(
            RetrieverCall(method, user_id, space_id, list(concept_ids or []), ok)
        )
        if not ok:
            self.isolation_violations.append(
                {
                    "method": method,
                    "requestUserId": user_id,
                    "requestSpaceId": space_id,
                    "boundUserId": self._data.user_id,
                    "boundSpaceId": self._data.space_id,
                }
            )

    def _knowledge_chunks(self, query: KnowledgeQuery) -> list[KnowledgeChunk]:
        wanted = set(query.concept_ids)
        chunks = [item for item in self._data.knowledge if item.concept_id in wanted] if wanted else []
        if not chunks and query.question:
            chunks = [
                item
                for item in self._data.knowledge
                if any(token and token in item.content for token in query.question.split())
            ]
        return chunks[: min(query.top_k, self._knowledge_top_k)]


# ===================== 加载与 profile =====================


def load_fixture(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    users = raw.get("users")
    if not isinstance(users, list) or len(users) < 2:
        raise ValueError("Phase 1 fixture 必须至少包含 2 个用户")
    return raw


def fixture_manifest(raw: dict) -> dict:
    users = raw["users"]
    spaces = [(user["userId"], space) for user in users for space in user["spaces"]]
    documents = [
        (space["spaceId"], document)
        for _, space in spaces
        for document in space.get("documents", [])
    ]
    return {
        "users": len(users),
        "spaces": len(spaces),
        "documents": len(documents),
        "userIds": [user["userId"] for user in users],
        "spaceIds": [space["spaceId"] for _, space in spaces],
        "documentNames": sorted({document["name"] for _, document in documents}),
        "nodes": [
            {
                "userId": user_id,
                "spaceId": space["spaceId"],
                "concepts": len(space.get("concepts", [])),
                "documents": len(space.get("documents", [])),
            }
            for user_id, space in spaces
        ],
    }


def make_retriever(
    raw: dict,
    *,
    user_id: int,
    space_id: int,
    profile: str,
    memory_top_k: int = 5,
    knowledge_top_k: int = 5,
) -> EvalRetriever:
    if profile not in PROFILES:
        raise ValueError(f"未知 profile {profile!r}")
    space = _find_space(raw, user_id, space_id)
    data = _parse_space(user_id, space)
    data = _apply_profile(data, profile)
    override = _KNOWLEDGE_OVERRIDES.get(profile, {})
    return EvalRetriever(
        data,
        profile=profile,
        memory_top_k=memory_top_k,
        knowledge_top_k=knowledge_top_k,
        **override,
    )


def _find_space(raw: dict, user_id: int, space_id: int) -> dict:
    for user in raw["users"]:
        if user["userId"] != user_id:
            continue
        for space in user["spaces"]:
            if space["spaceId"] == space_id:
                return space
    raise ValueError(f"fixture 中找不到 user={user_id} space={space_id}")


def _parse_space(user_id: int, space: dict) -> SpaceData:
    return SpaceData(
        user_id=user_id,
        space_id=int(space["spaceId"]),
        name=str(space.get("name", "")),
        concepts=[Concept.model_validate(item) for item in space.get("concepts", [])],
        understandings=[
            Understanding.model_validate(item) for item in space.get("understandings", [])
        ],
        memories=[MemoryItem.model_validate(item) for item in space.get("memories", [])],
        knowledge=[
            KnowledgeChunk.model_validate(item) for item in space.get("knowledge", [])
        ],
        documents=copy.deepcopy(space.get("documents", [])),
        evidences=[
            EvidenceItem.model_validate(item) for item in space.get("evidences", [])
        ],
        revisions=[
            UnderstandingRevision.model_validate(item)
            for item in space.get("revisions", [])
        ],
    )


_KNOWLEDGE_OVERRIDES: dict[str, dict] = {
    "alice_no_valid_documents": {
        "knowledge_status": "NO_VALID_DOCUMENTS",
        "knowledge_warning": "空间内没有已就绪（READY）的文档。",
    },
    "alice_empty_history": {
        "knowledge_status": "NO_VALID_DOCUMENTS",
        "knowledge_warning": "空间内没有已就绪（READY）的文档。",
    },
    "alice_no_match": {
        "knowledge_status": "NO_MATCH",
        "knowledge_warning": "知识检索没有命中任何片段。",
    },
    "alice_threshold_filtered": {
        "knowledge_status": "THRESHOLD_FILTERED",
        "knowledge_warning": "候选片段相关度低于阈值。",
        "knowledge_candidates": 3,
    },
    "alice_degraded": {
        "knowledge_status": "DEGRADED",
        "knowledge_warning": "Embedding / 向量检索不可用。",
    },
    "alice_non_ready": {
        "knowledge_status": "NO_VALID_DOCUMENTS",
        "knowledge_warning": "唯一文档 broken.md 的索引状态是 FAILED。",
    },
}


def _apply_profile(data: SpaceData, profile: str) -> SpaceData:
    data = copy.deepcopy(data)
    if profile in ("alice_current", "bob_current", "alice_db_space"):
        if profile == "alice_current":
            data.revisions = [item for item in data.revisions if item.revision_no <= 2]
        return data

    if profile == "alice_empty_history":
        data.understandings = []
        data.memories = []
        data.evidences = []
        data.revisions = []
        return data

    if profile == "alice_partial":
        return data

    if profile == "alice_non_ready":
        data.documents = [item for item in data.documents if item["status"] != "READY"]
        data.knowledge = []
        return data

    if profile == "alice_budget":
        data.memories.append(
            MemoryItem(
                id=1409,
                scope_type=MemoryScopeType.SPACE,
                learning_space_id=data.space_id,
                memory_type="PREFERENCE",
                memory_key="budget-padding",
                content=_BUDGET_MEMORY,
                status=MemoryStatus.ACTIVE,
            )
        )
        return data

    if profile in ("alice_corrected", "alice_conflict_memory", "alice_explicit_annotation", "alice_citation"):
        _set_corrected(data)
        if profile == "alice_explicit_annotation":
            if not any(item.id == 1703 for item in data.evidences):
                raise ValueError("显式批注 profile 缺少 evidence 1703")
        return data

    if profile == "alice_evidence_only":
        _set_corrected(data)
        # 保留 evidence，删除 revision：模拟「只有按需召回证据、没有轨迹」。
        data.revisions = []
        return data

    return data


def _set_corrected(data: SpaceData) -> None:
    revision = next((item for item in data.revisions if item.revision_no == 3), None)
    if revision is None:
        raise ValueError("corrected profile 需要 revision_no=3")
    current = next((item for item in data.understandings if item.concept_id == 9201), None)
    if current is None:
        raise ValueError("corrected profile 需要 concept 9201 的 Understanding")
    current.summary = revision.summary
    current.known_parts = list(revision.known_parts)
    current.misconceptions = [
        Misconception.model_validate(item.model_dump()) for item in revision.misconceptions
    ]
    current.uncertain_parts = list(revision.uncertain_parts)
    current.latest_user_explanation = revision.user_explanation
    current.status = revision.status
    current.current_revision_id = revision.id
    current.updated_at = revision.created_at
