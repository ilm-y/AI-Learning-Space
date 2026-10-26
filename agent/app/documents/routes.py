"""内部文档解析 / 索引 HTTP 接口。

Spring Boot 通过 `X-Internal-Token` + `X-Internal-User-Id` 调用；这两个接口
不接受普通用户 JWT，也绝不直接暴露给前端。

响应统一为 `{"code": 0, "data": ...}`；失败时 HTTP 状态码和 `code` 都非 0，
确保后端能把文档置为 FAILED，而不是误以为索引成功。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.config import Settings
from app.documents.auth import require_internal_user
from app.documents.errors import DocumentError, DocumentVectorStoreError
from app.documents.parser import parse_document
from app.documents.schemas import IndexDocumentRequest
from app.embedding.base import validate_vectors
from app.vectorstore.qdrant_store import ChunkVector, QdrantDocumentStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/documents", tags=["documents"])


@router.post("/parse")
async def parse(
    request: Request,
    internal_user_id: int = Depends(require_internal_user),
    document_id: int = Form(..., alias="documentId"),
    learning_space_id: int = Form(..., alias="learningSpaceId"),
    user_id: int = Form(..., alias="userId"),
    file: UploadFile = File(...),
) -> JSONResponse:
    settings: Settings = request.app.state.settings

    if document_id <= 0 or learning_space_id <= 0 or user_id <= 0:
        return _error(422, "documentId / learningSpaceId / userId 必须为正整数")
    if user_id != internal_user_id:
        return _error(403, "请求 userId 与内部凭据不一致")
    if not file.filename:
        return _error(400, "文件名不能为空")

    try:
        data = await file.read(settings.document_max_bytes + 1)
        parsed = parse_document(
            file.filename,
            data,
            max_bytes=settings.document_max_bytes,
            max_chars=settings.document_chunk_max_chars,
            overlap_chars=settings.document_chunk_overlap_chars,
        )
    except DocumentError as exc:
        return _error(exc.status_code, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("文档解析未预期失败: documentId=%s", document_id)
        return _error(500, f"Agent 内部错误：{type(exc).__name__}")

    return JSONResponse(content={"code": 0, "data": parsed.as_response()})


@router.post("/index")
async def index(
    payload: IndexDocumentRequest,
    request: Request,
    internal_user_id: int = Depends(require_internal_user),
) -> JSONResponse:
    if payload.user_id != internal_user_id:
        return _error(403, "请求 userId 与内部凭据不一致")

    provider = request.app.state.embedding_provider
    store: QdrantDocumentStore = request.app.state.qdrant_store

    try:
        texts = [chunk.content for chunk in payload.chunks]
        vectors = await provider.embed(texts)
        vectors = validate_vectors(
            vectors,
            expected_count=len(texts),
            dimension=provider.dimension,
            provider=provider.name,
        )
        collection = await store.ensure_collection(
            model=provider.model,
            dimension=provider.dimension,
        )
        chunk_vectors = [
            ChunkVector(
                chunk_id=chunk.chunk_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                vector=vectors[index],
                metadata=chunk.metadata,
            )
            for index, chunk in enumerate(payload.chunks)
        ]
        mapping = await store.index_document(
            collection_name=collection,
            document_id=payload.document_id,
            user_id=payload.user_id,
            space_id=payload.space_id,
            document_name=payload.document_name,
            file_name=payload.file_name,
            file_type=payload.file_type,
            chunks=chunk_vectors,
        )
        if len(mapping) != len(payload.chunks):
            raise DocumentVectorStoreError("Qdrant 返回的 point 数量与 chunk 数量不一致")
    except DocumentError as exc:
        return _error(exc.status_code, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "文档索引未预期失败: documentId=%s, chunks=%s",
            payload.document_id,
            len(payload.chunks),
        )
        return _error(500, f"Agent 内部错误：{type(exc).__name__}")

    return JSONResponse(
        content={
            "code": 0,
            "data": {
                "collection": collection,
                "embeddingModel": provider.model,
                "embeddingProvider": provider.name,
                "vectors": [
                    {"chunkId": chunk.chunk_id, "pointId": mapping[chunk.chunk_id]}
                    for chunk in payload.chunks
                ],
            },
        }
    )


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message},
    )
