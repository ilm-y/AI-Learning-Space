"""Retriever —— 把「跨对话的用户认知」读进本次运行。

对应 `agent-architecture.md` 第八节的三条检索线（Knowledge / Understanding / Memory）
加上按需召回的 Evidence。第一版不用向量库：

```text
Concept 识别（规则版命中名称）
        ↓
Understanding  Retrieval   ← 我现在该怎么讲
Memory         Retrieval   ← 这个用户长期值得记住什么
Knowledge      Retrieval   ← 我需要参考什么知识（第一版是占位，见下）
Evidence       Retrieval   ← 为什么系统这样判断（按需，不要每次都灌）
```

三条线都是**失败可降级**的：检索不到不是错误，只是这次个性化弱一些。
所以实现里大量使用「返回空列表」而不是抛异常 —— 只有真正连不上后端时才会向上报。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas import Concept, EvidenceItem, KnowledgeChunk, MemoryItem, Understanding


class RetrievalError(RuntimeError):
    """检索源整体不可用（后端连不上 / JWT 失效 / 业务错误）。

    「这个 Concept 没有快照」不是错误，不该抛这个 —— 那属于正常返回空。
    """


@runtime_checkable
class Retriever(Protocol):
    @property
    def name(self) -> str:
        """检索源标识，进日志与 /health。"""

    async def list_concepts(self, space_id: int) -> list[Concept]:
        """空间下的全部 Concept —— 同时是概念识别的候选集。"""
        ...

    async def get_understandings(
        self, user_id: int, space_id: int, concept_ids: list[int]
    ) -> list[Understanding]:
        """用户在这些 Concept 上的当前认知快照。"""
        ...

    async def get_memories(
        self, user_id: int, space_id: int, concept_ids: list[int], query: str
    ) -> list[MemoryItem]:
        """与当前话题相关的长期 Memory。只取 ACTIVE —— CANDIDATE 还没被用户确认。"""
        ...

    async def get_knowledge(
        self, space_id: int, concept_ids: list[int], query: str
    ) -> list[KnowledgeChunk]:
        """参考知识。

        第一版返回空列表：文档与向量检索是 M6 的事（`document` / `document_chunk`
        两张表已在位，但后端接口还是 501）。接口先占住，接 RAG 时上层不用改。
        """
        ...

    async def get_evidences(
        self, user_id: int, concept_ids: list[int], limit: int
    ) -> list[EvidenceItem]:
        """判定依据。按需召回，默认条数很小 —— 不是每次回答都需要它。"""
        ...

    async def aclose(self) -> None:
        """释放底层资源。"""
        ...
