"""记忆检索的降级状态解析测试。

Agent 必须调用带 ``status`` 的混合检索端点：旧 ``/api/memories/search`` 只返回
列表，检索整条链路不可用时和「确实没有记忆」都是 ``[]``，无法区分。
这里用 ``httpx.MockTransport`` 模拟后端响应，不连接真实服务。
"""

from __future__ import annotations

import logging

import httpx

from app.retriever.base import RetrievalStatus
from app.retriever.http_backend import HttpBackendRetriever


def _memory(memory_id: int = 11, content: str = "递归三要素") -> dict:
    return {
        "id": memory_id,
        "scopeType": "GLOBAL",
        "memoryType": "LEARNING",
        "content": content,
        "status": "ACTIVE",
        "importance": 0.5,
    }


def _hybrid_data(status: str, items: list[dict], warnings: list[str] | None = None) -> dict:
    return {
        "status": status,
        "items": items,
        "warnings": warnings or [],
        "elapsedMs": 4,
    }


def _retriever(handler) -> HttpBackendRetriever:
    retriever = HttpBackendRetriever(
        "http://backend.test", auth_token="user-jwt", memory_top_k=5
    )
    retriever._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-jwt"},
    )
    return retriever


async def test_memories_use_hybrid_endpoint_and_parse_nested_memory() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["limit"] = request.url.params.get("limit")
        seen["keyword"] = request.url.params.get("keyword")
        seen["conceptId"] = request.url.params.get("conceptId")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": _hybrid_data(
                    "OK",
                    [{"memory": _memory(11)}, {"memory": _memory(12)}],
                ),
            },
        )

    retriever = _retriever(handler)
    try:
        result = await retriever.get_memories(7, 9, [3], "递归怎么写")
    finally:
        await retriever.aclose()

    assert seen["path"] == "/api/memories/search/hybrid"
    assert seen["limit"] == "5"
    assert seen["keyword"] == "递归怎么写"
    assert seen["conceptId"] == "3"
    assert [memory.id for memory in result.items] == [11, 12]
    assert result.status is RetrievalStatus.SUCCESS


async def test_unavailable_status_is_logged_instead_of_silently_empty(caplog) -> None:
    warnings = ["词法检索失败：DB down", "向量检索失败：Agent down"]

    retriever = _retriever(
        lambda request: httpx.Response(
            200,
            json={"code": 0, "data": _hybrid_data("UNAVAILABLE", [], warnings)},
        )
    )
    try:
        with caplog.at_level(logging.WARNING, logger="app.retriever.http_backend"):
            result = await retriever.get_memories(7, 9, [], "递归")
    finally:
        await retriever.aclose()

    assert result.items == []
    assert result.status is RetrievalStatus.DEGRADED
    assert result.detail == "UNAVAILABLE"
    assert "无法确认" in (result.warning or "")
    assert "UNAVAILABLE" in caplog.text
    assert "记忆检索不可用" in caplog.text
    assert "向量检索失败" in caplog.text


async def test_degraded_status_returns_items_but_logs_warning(caplog) -> None:
    retriever = _retriever(
        lambda request: httpx.Response(
            200,
            json={
                "code": 0,
                "data": _hybrid_data(
                    "DEGRADED_VECTOR",
                    [{"memory": _memory(21)}],
                    ["向量检索失败：Agent 不可用"],
                ),
            },
        )
    )
    try:
        with caplog.at_level(logging.WARNING, logger="app.retriever.http_backend"):
            result = await retriever.get_memories(7, 9, [], "递归")
    finally:
        await retriever.aclose()

    assert [memory.id for memory in result.items] == [21]
    assert result.status is RetrievalStatus.DEGRADED
    assert result.detail == "DEGRADED_VECTOR"
    assert "不能据此判断" in (result.warning or "")
    assert "DEGRADED_VECTOR" in caplog.text


async def test_missing_status_is_reported_but_keeps_parsable_items(caplog) -> None:
    # 旧格式/网关裁剪：items 仍是合法结构，但没有 status。
    retriever = _retriever(
        lambda request: httpx.Response(
            200,
            json={"code": 0, "data": {"items": [{"memory": _memory(31)}]}},
        )
    )
    try:
        with caplog.at_level(logging.WARNING, logger="app.retriever.http_backend"):
            result = await retriever.get_memories(7, 9, [], "递归")
    finally:
        await retriever.aclose()

    assert [memory.id for memory in result.items] == [31]
    assert result.status is RetrievalStatus.DEGRADED
    assert result.detail == "UNKNOWN"
    assert "缺少 status" in caplog.text
