"""文档解析 / 向量索引的显式错误。

这里不使用「返回空结果」或「降级成 Hash Embedding」之类的隐式兜底：
Spring Boot 必须能从 HTTP 状态码和消息里判断本次索引失败，从而把文档置为 FAILED。
"""

from __future__ import annotations


class DocumentError(Exception):
    """文档链路错误的基类，携带建议的 HTTP 状态码。"""

    status_code = 500

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class DocumentParseError(DocumentError):
    """文件扩展名、编码、大小或内容不合法。"""

    status_code = 400


class DocumentPayloadTooLarge(DocumentParseError):
    status_code = 413


class DocumentEmbeddingError(DocumentError):
    """Embedding 服务失败，或返回数量 / 维度 / 数值不合法。"""

    status_code = 502


class DocumentEmbeddingUnavailable(DocumentEmbeddingError):
    status_code = 503


class DocumentVectorStoreError(DocumentError):
    """Qdrant 不可用、集合维度不一致或写入失败。"""

    status_code = 502


class DocumentVectorStoreUnavailable(DocumentVectorStoreError):
    status_code = 503


class DocumentRequestError(DocumentError):
    """内部索引请求本身不合法。"""

    status_code = 422
