"""文档解析、切块与内部接口。"""

from app.documents.errors import (
    DocumentEmbeddingError,
    DocumentEmbeddingUnavailable,
    DocumentError,
    DocumentParseError,
    DocumentPayloadTooLarge,
    DocumentRequestError,
    DocumentVectorStoreError,
    DocumentVectorStoreUnavailable,
)
from app.documents.parser import ParsedDocument, parse_document

__all__ = [
    "DocumentEmbeddingError",
    "DocumentEmbeddingUnavailable",
    "DocumentError",
    "DocumentParseError",
    "DocumentPayloadTooLarge",
    "DocumentRequestError",
    "DocumentVectorStoreError",
    "DocumentVectorStoreUnavailable",
    "ParsedDocument",
    "parse_document",
]
