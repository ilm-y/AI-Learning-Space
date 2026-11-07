"""记忆向量同步 / 检索的请求模型。

字段别名与 Spring Boot DTO 的 camelCase 契约逐字对应；`populate_by_name=True`
让 Python 测试和内部脚本可以直接用 snake_case。`extra="forbid"` 让两端契约
漂移立即失败，而不是静默丢弃字段。

`operation` 是设计文档中的规范字段名；为了兼容调用方使用 `action` 的写法，
两者都接受，但必须且只能出现一个。统一在模型校验后归一化成 `operation`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_OPERATIONS = {"UPSERT", "DELETE"}
_SCOPE_TYPES = {"GLOBAL", "SPACE", "CONCEPT"}


class MemorySyncRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    event_id: int | None = Field(alias="eventId", default=None, ge=1)
    # 规范字段名；`action` 是兼容别名，见 model_validator。
    operation: str | None = None
    action: str | None = None

    memory_id: int = Field(alias="memoryId", gt=0)
    user_id: int = Field(alias="userId", gt=0)
    content: str | None = None
    content_hash: str | None = Field(alias="contentHash", default=None, max_length=128)
    memory_version: int | None = Field(alias="memoryVersion", default=None, ge=0)
    scope_type: str | None = Field(alias="scopeType", default=None)
    learning_space_id: int | None = Field(alias="learningSpaceId", default=None, gt=0)
    concept_id: int | None = Field(alias="conceptId", default=None, gt=0)
    memory_type: str | None = Field(alias="memoryType", default=None, max_length=32)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str = Field(default="ACTIVE", max_length=32)

    @model_validator(mode="before")
    @classmethod
    def _normalize_operation(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        operation = data.get("operation")
        action = data.get("action")
        if operation and action and str(operation).strip().upper() != str(action).strip().upper():
            raise ValueError("operation 与 action 不一致")
        if "operation" not in data and action is not None:
            data = dict(data)
            data["operation"] = action
        return data

    @field_validator("operation", mode="before")
    @classmethod
    def _operation_supported(cls, value: object) -> str:
        if value is None:
            raise ValueError("缺少 operation（或兼容字段 action）")
        normalized = str(value).strip().upper()
        if normalized not in _OPERATIONS:
            raise ValueError("operation 只支持 UPSERT / DELETE")
        return normalized

    @field_validator("scope_type")
    @classmethod
    def _scope_supported(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().upper()
        if normalized not in _SCOPE_TYPES:
            raise ValueError("scopeType 只支持 GLOBAL / SPACE / CONCEPT")
        return normalized

    @field_validator("content")
    @classmethod
    def _content_stripped(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _validate_operation_payload(self) -> "MemorySyncRequest":
        # `action` 只作为输入别名，归一化后不再保留。
        self.action = None
        if self.operation is None:
            raise ValueError("缺少 operation（或兼容字段 action）")
        if self.operation == "UPSERT" and not self.content:
            raise ValueError("UPSERT 的 content 不能为空")
        if self.concept_id is not None and self.learning_space_id is None:
            raise ValueError("conceptId 存在时 learningSpaceId 不能为空")
        if self.scope_type is None:
            # 最小契约只要求 memoryId / userId / content / operation；作用域
            # 缺失时按可用字段推导。Spring Relay 总是显式传入 scopeType。
            if self.concept_id is not None:
                self.scope_type = "CONCEPT"
            elif self.learning_space_id is not None:
                self.scope_type = "SPACE"
            else:
                self.scope_type = "GLOBAL"
        if self.scope_type in {"SPACE", "CONCEPT"} and self.learning_space_id is None:
            raise ValueError(f"scopeType={self.scope_type} 时 learningSpaceId 不能为空")
        if self.scope_type == "CONCEPT" and self.concept_id is None:
            raise ValueError("scopeType=CONCEPT 时 conceptId 不能为空")
        return self

    @property
    def is_delete(self) -> bool:
        return self.operation == "DELETE"


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    request_id: str | None = Field(alias="requestId", default=None, max_length=64)
    user_id: int = Field(alias="userId", gt=0)
    space_id: int | None = Field(alias="spaceId", default=None, gt=0)
    concept_id: int | None = Field(alias="conceptId", default=None, gt=0)
    query: str = Field(min_length=1, max_length=2000)
    recall_k: int | None = Field(alias="recallK", default=None, ge=1, le=200)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query 不能为空")
        return stripped

    @model_validator(mode="after")
    def _concept_requires_space(self) -> "MemorySearchRequest":
        if self.concept_id is not None and self.space_id is None:
            raise ValueError("conceptId 存在时 spaceId 不能为空")
        return self
