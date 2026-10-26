"""Fixture Retriever —— 从本地 JSON 读一份样例认知数据。

存在的理由有三个，都不是「假装后端已经跑起来了」：
1. **离线可跑**：没有 MySQL / 没有登录态时，整条流水线仍然能演示与单测；
2. **可回归**：测试需要一份固定输入，而真实数据库是会变的；
3. **是文档**：`fixtures/demo_space.json` 用 README 里那个 TLB 场景，
   把「Understanding / Memory / Knowledge 长什么样」写成了可执行的例子。

数据形状与后端响应完全一致（同一个 Pydantic 模型），
所以从 Fixture 切到 HTTP 不需要改上层任何代码。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.schemas import (
    Concept,
    EvidenceItem,
    KnowledgeChunk,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    Understanding,
)


class FixtureRetriever:
    def __init__(self, fixture_file: Path, *, memory_top_k: int = 5, knowledge_top_k: int = 5) -> None:
        self._file = fixture_file
        self._memory_top_k = memory_top_k
        self._knowledge_top_k = knowledge_top_k
        self._loaded = False
        self._concepts: list[Concept] = []
        self._understandings: list[Understanding] = []
        self._memories: list[MemoryItem] = []
        self._knowledge: list[KnowledgeChunk] = []
        self._evidences: list[EvidenceItem] = []
        self.space_id: int | None = None
        self.space_name: str = ""

    @property
    def name(self) -> str:
        return f"fixture:{self._file.name}"

    # ---------- 检索 ----------

    async def list_concepts(self, space_id: int) -> list[Concept]:
        self._ensure_loaded()
        return list(self._concepts)

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        self._ensure_loaded()
        if not concept_ids:
            return []
        wanted = set(concept_ids)
        return [item for item in self._understandings if item.concept_id in wanted]

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        self._ensure_loaded()
        wanted = set(concept_ids)
        active = [item for item in self._memories if item.status is MemoryStatus.ACTIVE]
        # 可见性按后端 §二十一 的同一套规则（这里用 scope_type 表达，不再靠 concept_id 是否为空来猜）：
        # GLOBAL 恒可见；SPACE 需要 space_id 对得上；CONCEPT 还要 concept_id 落在本轮命中的概念里。
        # Fixture 与后端行为一致，才能在不起后端时用 mock 检索线验证 prompt 里到底有什么。
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
        self._ensure_loaded()
        wanted = set(query.concept_ids)
        chunks = [item for item in self._knowledge if item.concept_id in wanted] if wanted else []
        # 没有 Concept 命中时按关键词给一点兜底，模拟真实检索里「问题本身也能召回」。
        if not chunks and query.question:
            chunks = [
                item for item in self._knowledge if _rough_match(item.content, query.question)
            ]
        limit = min(query.top_k, self._knowledge_top_k)
        chunks = chunks[:limit]
        return KnowledgeResult(
            chunks=chunks,
            status=KnowledgeStatus.SUCCESS if chunks else KnowledgeStatus.NO_MATCH,
        )

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        self._ensure_loaded()
        if not concept_ids:
            return []
        wanted = set(concept_ids)
        items = [item for item in self._evidences if item.concept_id in wanted]
        return items[-limit:] if limit > 0 else []

    async def aclose(self) -> None:
        return None

    # ---------- 载入 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._file.exists():
            raise FileNotFoundError(
                f"找不到 Fixture 数据：{self._file}。"
                "检查 AILS_FIXTURE_PATH，或把 AILS_RETRIEVER_MODE 设成 http 并传 authToken。"
            )
        raw = json.loads(self._file.read_text(encoding="utf-8"))
        space = raw.get("space") or {}
        self.space_id = space.get("id")
        self.space_name = space.get("name", "")

        self._concepts = [Concept.model_validate(item) for item in raw.get("concepts", [])]
        self._understandings = [
            Understanding.model_validate(item) for item in raw.get("understandings", [])
        ]
        self._memories = [MemoryItem.model_validate(item) for item in raw.get("memories", [])]
        self._knowledge = [KnowledgeChunk.model_validate(item) for item in raw.get("knowledge", [])]
        self._evidences = [EvidenceItem.model_validate(item) for item in raw.get("evidences", [])]
        self._loaded = True


def _rough_match(content: str, query: str) -> bool:
    return any(token and token in content for token in query.split())
