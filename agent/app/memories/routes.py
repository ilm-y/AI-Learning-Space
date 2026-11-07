"""内部记忆向量接口。

与文档接口共用同一套鉴权：`X-Internal-Token` + 签名身份
（`X-Internal-Identity`）。请求体里的 `userId` 必须与签名身份一致，
否则 403 —— 共享 token 本身不构成用户身份。

响应统一为 `{"code": 0, "data": ...}`；失败时 HTTP 状态码与 `code` 都非 0，
Spring 的 Outbox Relay 据此区分可重试与永久失败。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.internal_auth import require_internal_user
from app.memories.errors import MemoryError
from app.memories.indexer import MemoryIndexer
from app.memories.schemas import MemorySearchRequest, MemorySyncRequest
from app.memories.search import MemoryVectorSearch

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/memories", tags=["memories"])


@router.post("/sync")
async def sync(
    payload: MemorySyncRequest,
    request: Request,
    internal_user_id: int = Depends(require_internal_user),
) -> JSONResponse:
    if payload.user_id != internal_user_id:
        return _error(403, "请求 userId 与内部身份不一致")

    indexer: MemoryIndexer = request.app.state.memory_indexer
    try:
        data = await indexer.sync(payload)
    except MemoryError as exc:
        return _error(exc.status_code, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "记忆向量同步未预期失败: memoryId=%s operation=%s",
            payload.memory_id,
            payload.operation,
        )
        return _error(500, f"Agent 内部错误：{type(exc).__name__}")

    return JSONResponse(content={"code": 0, "data": data})


@router.post("/search")
async def search(
    payload: MemorySearchRequest,
    request: Request,
    internal_user_id: int = Depends(require_internal_user),
) -> JSONResponse:
    if payload.user_id != internal_user_id:
        return _error(403, "请求 userId 与内部身份不一致")

    searcher: MemoryVectorSearch = request.app.state.memory_search
    try:
        data = await searcher.search(payload)
    except MemoryError as exc:
        return _error(exc.status_code, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "记忆向量检索未预期失败: requestId=%s", payload.request_id
        )
        return _error(500, f"Agent 内部错误：{type(exc).__name__}")

    return JSONResponse(content={"code": 0, "data": data})


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message},
    )
