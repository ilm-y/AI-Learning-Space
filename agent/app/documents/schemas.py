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
    # 构建令牌；0 表示迁移前/旧客户端，沿用 legacy point ID 公式。
    index_version: int = Field(alias="indexVersion", default=0, ge=0)
    chunks: list[IndexChunk] = Field(min_length=1, max_length=10_000)

    @field_validator("file_type")
    @classmethod
    def file_type_supported(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"md", "txt"}:
            raise ValueError("仅支持 md / txt")
        return normalized

    @model_validator(mode="after")
    def unique_chunks_ok(self) -> "IndexDocumentRequest":
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        indexes = [chunk.chunk_index for chunk in self.chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("chunkId 不能重复")
        if len(set(indexes)) != len(indexes):
            raise ValueError("chunkIndex 不能重复")
        if sorted(indexes) != list(range(len(indexes))):
            raise ValueError("chunkIndex 必须从 0 连续递增")
        return self


class VectorCountRequest(BaseModel):
    """反向对账：统计某文档某版本在指定 collection 中的 point 数。

    `collection` 必须显式提供：后端在 legacy 文档无法定位 collection 时不会调用
    本接口，Agent 也不应按“当前 provider 配置”猜测，否则会把对账变成误报。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document_id: int = Field(alias="documentId", gt=0)
    collection: str = Field(min_length=1, max_length=255)
    index_version: int = Field(alias="indexVersion", ge=0)

    @field_validator("collection")
    @classmethod
    def collection_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("collection 不能为空")
        return stripped


class DeleteDocumentRequest(BaseModel):
    """删除某文档的向量。

    - `keepIndexVersion is None`：删除全部版本（文档删除）。
    - `keepIndexVersion` 非空：保留该活跃版本，清理其余版本（切换后的延迟清理）。
    - `indexVersions` 非空：只删除这些精确版本；`deleteLegacy` 同时删除没有
      `index_version` 字段的迁移前 points。它与 `keepIndexVersion` 互斥。
    - `collection` 为空时按当前 Embedding 配置计算，兼容迁移前未记录 collection 的文档。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document_id: int = Field(alias="documentId", gt=0)
    collection: str | None = Field(default=None, max_length=255)
    embedding_model: str | None = Field(alias="embeddingModel", default=None)
    embedding_provider: str | None = Field(alias="embeddingProvider", default=None)
    embedding_dimension: int | None = Field(alias="embeddingDimension", default=None, gt=0)
    embedding_fingerprint: str | None = Field(alias="embeddingFingerprint", default=None)
    keep_index_version: int | None = Field(alias="keepIndexVersion", default=None, ge=0)
    index_versions: list[int] | None = Field(alias="indexVersions", default=None)
    delete_legacy: bool = Field(alias="deleteLegacy", default=False)

    @field_validator("collection", "embedding_model", "embedding_provider",
                     "embedding_fingerprint")
    @classmethod
    def blank_as_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def delete_mode_is_exclusive(self) -> "DeleteDocumentRequest":
        if self.keep_index_version is not None and self.index_versions is not None:
            raise ValueError("keepIndexVersion 与 indexVersions 不能同时提供")
        if self.index_versions is not None:
            if not self.index_versions:
                raise ValueError("indexVersions 不能为空")
            if any(version < 0 for version in self.index_versions):
                raise ValueError("indexVersions 不能包含负数")
        return self
