"""记忆向量链路的显式错误。

与文档索引一样，失败必须带一个可判定的 HTTP 状态码：Spring 的 Outbox Relay
依赖它区分「可重试」（5xx / 超时）与「永久失败」（4xx 契约错误）。
"""

from __future__ import annotations


class MemoryError(Exception):
    """记忆向量链路错误基类，携带建议的 HTTP 状态码。"""

    status_code = 500

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class MemoryRequestError(MemoryError):
    """请求本身不合法（缺字段、非法 operation 等）。"""

    status_code = 422


class MemoryEmbeddingError(MemoryError):
    """Embedding 服务失败，或返回数量 / 维度 / 数值不合法。"""

    status_code = 502


class MemoryEmbeddingUnavailable(MemoryEmbeddingError):
    status_code = 503


class MemoryVectorStoreError(MemoryError):
    """Qdrant 不可用、集合维度不一致或写入失败。"""

    status_code = 502


class MemoryVectorStoreUnavailable(MemoryVectorStoreError):
    status_code = 503
