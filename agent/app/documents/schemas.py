"""内部文档接口的请求模型。

字段别名与 Spring Boot DTO 的 camelCase 契约逐字对应；同时允许 snake_case
（`populate_by_name=True`），方便 Python 测试直接构造。`extra="forbid"` 让
契约漂移立即暴露，而不是静默忽略未知字段。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IndexChunk(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    chunk_id: int = Field(alias="chunkId", gt=0)
    chunk_index: int = Field(alias="chunkIndex", ge=0)
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chunk content 不能为空")
        return value


class IndexDocumentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document_id: int = Field(alias="documentId", gt=0)
    user_id: int = Field(alias="userId", gt=0)
    space_id: int = Field(alias="spaceId", gt=0)
    document_name: str = Field(alias="documentName", min_length=1, max_length=255)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    file_type: str = Field(alias="fileType")
    chunks: list[IndexChunk] = Field(min_length=1, max_length=10_000)

    @field_validator("file_type")
    @classmethod
    def file_type_supported(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"md", "txt"}:
            raise ValueError("仅支持 md / txt")
        return normalized

    @model_validator(mode="after")
    def unique_chunks(self) -> "IndexDocumentRequest":
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        indexes = [chunk.chunk_index for chunk in self.chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("chunkId 不能重复")
        if len(set(indexes)) != len(indexes):
            raise ValueError("chunkIndex 不能重复")
        if sorted(indexes) != list(range(len(indexes))):
            raise ValueError("chunkIndex 必须从 0 连续递增")
        return self
