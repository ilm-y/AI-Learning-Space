"""真实链路 E2E（默认跳过）。

覆盖用户可见的完整路径：

```text
注册 / 创建 Space / 上传 Markdown
    → 后端调用 Agent 解析 + Embedding + Qdrant 索引
    → 轮询 status=READY
    → Agent Chat 问一个没有 Concept 的问题
    → 后端 ready-ids 白名单
    → Qdrant 过滤检索
    → Context Builder 来源标注
    → LLM 回答
```

启用前提（缺一不可）：

1. Spring Boot 后端已启动，且 `ails.agent.base-url` 指向正在运行的 Agent；
2. Agent 配置了**真实 Embedding Provider**（Hash 会被跳过，因为它不是语义检索）；
3. Qdrant 已启动，且 Agent/索引使用同一个 collection。

```bash
AILS_KNOWLEDGE_E2E=true \
AILS_E2E_BACKEND_URL=http://127.0.0.1:8080 \
python -m pytest tests/test_knowledge_e2e.py -v
```

测试使用随机用户与独立 Space；不会修改已有用户数据。
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import httpx
import pytest

from app.config import Settings
from app.embedding import build_embedding_provider
from app.llm.mock import MockLLMAdapter
from app.retriever.knowledge import KnowledgeSearchService
from app.runtime import AgentRuntime
from app.schemas import ChatRequest
from app.vectorstore.qdrant_store import QdrantDocumentStore

pytestmark = pytest.mark.skipif(
    os.getenv("AILS_KNOWLEDGE_E2E", "").lower() != "true",
    reason="设置 AILS_KNOWLEDGE_E2E=true 才运行真实链路 E2E",
)

BACKEND = os.getenv("AILS_E2E_BACKEND_URL", "http://127.0.0.1:8080")
PASSWORD = "e2e-password-123"
READY_TIMEOUT_SECONDS = float(os.getenv("AILS_E2E_READY_TIMEOUT", "90"))
SOURCE_LABEL = re.compile(r"\[资料《([^》]+)》#(\d+)\]")

DIRECT_FACT_DOC = """# 操作系统复习笔记

## 快表

TLB（Translation Lookaside Buffer，快表）是 MMU 内部的页表项缓存。
CPU 发出虚拟地址后先查 TLB：命中就直接得到物理页框号，未命中才进行页表遍历。
所以 TLB 不是 Page Table 本身，也不是缺页异常。

## 缺页异常

Page Fault 发生在页表项有效位表示该页不在物理内存时。
内核缺页处理程序把页面从磁盘换入，然后重新执行导致异常的那条指令。
"""

# 超过默认 max_chars=1200，会被切成多块；用于验证多片段共同参与回答。
MULTI_CHUNK_DOC = """# 地址翻译的三个阶段

## 第一阶段：TLB 命中

CPU 执行指令时发出虚拟地址，MMU 先用虚拟页号查询 TLB。TLB 是一张小而快的相联存储器，
里面缓存着最近使用过的页表项。TLB 命中意味着不需要访问内存中的页表，MMU 直接拿到
物理页框号，再把页内偏移拼接上去，得到最终物理地址。

TLB 命中是最快的路径。它没有任何异常，也不会触发操作系统内核介入。
如果程序具有很好的空间局部性，连续访问同一页内的数据，TLB 命中率会非常高。
这也是为什么循环遍历数组通常比随机访问链表快得多：前者反复命中同一批页表项。
需要强调，TLB 命中与否只影响地址翻译的代价，不改变程序看到的虚拟地址语义。

## 第二阶段：TLB 未命中与页表遍历

TLB 未命中并不等于页面不在内存。MMU 仍会按照页表基址寄存器指向的页表，
逐级进行页表遍历（page walk）。现代操作系统常使用多级页表，每一级都要读取
一个页表项，因此一次遍历可能产生多次内存访问。如果页表项存在且有效，
MMU 会把该页表项装入 TLB，然后重新执行本次地址翻译。

因此，TLB Miss 的代价通常只是一次或几次额外的内存读取；它属于缓存层级的未命中，
不是进程地址空间中真的缺页。把 TLB Miss 与 Page Fault 混为一谈，是操作系统学习里
最常见的误区之一。两者的触发条件、处理者和代价都不同，排查性能问题时必须分清。

## 第三阶段：缺页异常

当页表项的有效位表明该页不在物理内存中时，MMU 才会触发 Page Fault。
这时 CPU 陷入内核，缺页处理程序检查该虚拟地址是否合法：如果合法但页面在磁盘上，
就从磁盘或交换区读入页面、更新页表，再返回用户态重新执行导致异常的指令。
如果地址非法，内核会向进程发送段错误信号。

缺页异常的处理代价远高于 TLB Miss：它涉及内核态切换、磁盘 I/O 或页面回收，
还可能引发写时复制与页面置换。理解 TLB Miss、页表遍历与 Page Fault 的区别，
是分析虚拟内存性能问题的前提，也是区分硬件缓存与操作系统内存管理边界的关键。
"""


def _settings() -> Settings:
    settings = Settings()
    if settings.embedding_provider == "hash":
        pytest.skip("E2E 需要真实 Embedding Provider（AILS_EMBEDDING_PROVIDER != hash）")
    return settings


async def _auth(client: httpx.AsyncClient) -> tuple[str, int]:
    username = f"knowledge_e2e_{uuid.uuid4().hex[:10]}"
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD},
    )
    response.raise_for_status()
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]["token"], body["data"]["user"]["id"]


async def _create_space(client: httpx.AsyncClient, name: str) -> int:
    response = await client.post(
        "/api/spaces",
        json={"name": name, "description": "knowledge e2e", "visibility": "PRIVATE"},
    )
    response.raise_for_status()
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]["id"]


async def _upload_and_wait(
    client: httpx.AsyncClient, space_id: int, filename: str, content: str
) -> int:
    response = await client.post(
        f"/api/spaces/{space_id}/documents",
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
    )
    response.raise_for_status()
    body = response.json()
    assert body["code"] == 0, body
    document_id = body["data"]["document"]["id"]

    deadline = asyncio.get_running_loop().time() + READY_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        detail = (await client.get(f"/api/documents/{document_id}")).json()
        assert detail["code"] == 0, detail
        document = detail["data"]
        if document["status"] == "READY":
            assert document["chunkCount"] > 0
            return document_id
        if document["status"] == "FAILED":
            pytest.fail(f"文档索引失败：{document.get('indexError')}")
        await asyncio.sleep(1)
    pytest.fail(f"文档 {document_id} 在 {READY_TIMEOUT_SECONDS}s 内未进入 READY")


def _runtime(settings: Settings, search: KnowledgeSearchService) -> AgentRuntime:
    return AgentRuntime(
        settings.model_copy(update={"retriever_mode": "http"}),
        MockLLMAdapter(),
        knowledge_search=search,
    )


def _search(settings: Settings, provider, store) -> KnowledgeSearchService:
    return KnowledgeSearchService(
        provider=provider,
        store=store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )


def _request(space_id: int, user_id: int, token: str, question: str) -> ChatRequest:
    return ChatRequest(
        runId=1,
        userId=user_id,
        conversationId=1,
        learningSpaceId=space_id,
        userMessage=question,
        authToken=token,
    )


async def _ask(runtime: AgentRuntime, request: ChatRequest) -> tuple[str, dict]:
    deltas: list[str] = []
    meta: dict = {}
    final: dict = {}
    async for event in runtime.stream(request):
        if event.event == "meta":
            meta = event.data
        elif event.event == "delta":
            deltas.append(str(event.data["delta"]))
        elif event.event in {"done", "error"}:
            final = {"event": event.event, **event.data}
    return "".join(deltas), {"meta": meta, "final": final}


async def test_upload_then_answer_with_traceable_sources_and_degrade_safely() -> None:
    settings = _settings()
    provider = build_embedding_provider(settings)
    store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    search = _search(settings, provider, store)
    runtime = _runtime(settings, search)

    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(base_url=BACKEND, timeout=timeout) as client:
        token, user_id = await _auth(client)
        client.headers["Authorization"] = f"Bearer {token}"

        space_id = await _create_space(client, f"knowledge-e2e-{uuid.uuid4().hex[:8]}")
        empty_space_id = await _create_space(client, f"knowledge-e2e-empty-{uuid.uuid4().hex[:8]}")

        try:
            # 1) 直接事实题：Space 里没有任何 Concept，仍要检索到文档。
            await _upload_and_wait(client, space_id, "e2e-notes.md", DIRECT_FACT_DOC)
            direct = _request(space_id, user_id, token, "快表和页表是一回事吗？")
            prepared = await runtime.prepare(direct)
            assert prepared.state.concept_names == []
            assert prepared.state.knowledge_status == "SUCCESS"
            assert prepared.state.knowledge
            assert prepared.state.knowledge[0].document_name == "e2e-notes.md"

            answer, events = await _ask(runtime, direct)
            assert events["final"]["event"] == "done"
            labels = SOURCE_LABEL.findall(answer)
            assert labels, answer
            assert all(name == "e2e-notes.md" for name, _ in labels)
            # 只允许引用上下文里真实出现的 chunk 序号。
            allowed_indices = {str(chunk.chunk_index) for chunk in prepared.state.knowledge}
            assert {index for _, index in labels} <= allowed_indices

            # 2) 多片段题：同一个文档的多个 chunk 应一起参与。
            await _upload_and_wait(
                client, space_id, "e2e-multichunk.md", MULTI_CHUNK_DOC
            )
            multi = _request(
                space_id, user_id, token,
                "TLB 命中、TLB 未命中和缺页异常分别发生了什么？",
            )
            prepared_multi = await runtime.prepare(multi)
            assert prepared_multi.state.knowledge_status == "SUCCESS"
            multi_chunks = [
                chunk
                for chunk in prepared_multi.state.knowledge
                if chunk.document_name == "e2e-multichunk.md"
            ]
            assert len(multi_chunks) >= 2
            assert len({chunk.chunk_index for chunk in multi_chunks}) >= 2

            # 3) 无法回答的问题：不得伪造来源。
            unanswerable = _request(
                space_id, user_id, token,
                "请解释量子色动力学中的渐进自由。",
            )
            prepared_unanswerable = await runtime.prepare(unanswerable)
            assert prepared_unanswerable.state.knowledge == []
            assert prepared_unanswerable.state.knowledge_status in {
                "NO_MATCH",
                "THRESHOLD_FILTERED",
            }
            answer_unanswerable, events_unanswerable = await _ask(runtime, unanswerable)
            assert events_unanswerable["final"]["event"] == "done"
            assert not SOURCE_LABEL.search(answer_unanswerable)

            # 4) 没有任何可用文档：不查询、不伪造资料。
            prepared_empty = await runtime.prepare(
                _request(empty_space_id, user_id, token, "快表是什么？")
            )
            assert prepared_empty.state.knowledge_status == "NO_VALID_DOCUMENTS"
            assert prepared_empty.state.knowledge == []
            assert any("没有已就绪" in warning for warning in prepared_empty.context.warnings)

            # 5) Qdrant 不可用：/agent/chat 仍正常结束，只是没有知识。
            down_store = QdrantDocumentStore(
                url="http://127.0.0.1:9",
                api_key="",
                timeout_seconds=1,
                collection_prefix=settings.qdrant_collection_prefix,
                upsert_batch_size=settings.qdrant_upsert_batch_size,
            )
            down_runtime = _runtime(settings, _search(settings, provider, down_store))
            try:
                prepared_down = await down_runtime.prepare(
                    _request(space_id, user_id, token, "快表是什么？")
                )
                assert prepared_down.state.knowledge_status == "DEGRADED"
                assert prepared_down.state.knowledge == []
                _, events_down = await _ask(
                    down_runtime, _request(space_id, user_id, token, "快表是什么？")
                )
                assert events_down["final"]["event"] == "done"
            finally:
                await down_runtime.aclose()
                await down_store.aclose()

            # 6) Embedding 失败：同样降级为空，不抛 5xx。
            class BrokenProvider:
                name = "broken"
                model = provider.model
                dimension = provider.dimension

                async def embed(self, texts):
                    raise RuntimeError("embedding down")

                async def aclose(self):
                    return None

            broken_runtime = _runtime(settings, _search(settings, BrokenProvider(), store))
            try:
                prepared_broken = await broken_runtime.prepare(
                    _request(space_id, user_id, token, "快表是什么？")
                )
                assert prepared_broken.state.knowledge_status == "DEGRADED"
                assert prepared_broken.state.knowledge == []
            finally:
                await broken_runtime.aclose()
        finally:
            await runtime.aclose()
            await provider.aclose()
            await store.aclose()
