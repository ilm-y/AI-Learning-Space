"""`.md` / `.txt` 文档解析入口。

信任边界：文件名和 Content-Type 都来自跨服务调用，不能作为安全依据。
这里只按**服务端允许的扩展名**判断类型，对字节做严格 UTF-8 解码，
并统一处理 BOM 与 CRLF。任何异常都抛 `DocumentParseError`，由 HTTP 层转成
明确的 4xx，绝不会返回空 chunks 让 Spring 误判成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from app.documents.chunker import TextChunk, chunk_text
from app.documents.errors import DocumentParseError, DocumentPayloadTooLarge

PARSER_VERSION = "document-parser-v1"
ALLOWED_EXTENSIONS = {"md", "txt"}


@dataclass(frozen=True)
class ParsedDocument:
    parser_version: str
    chunks: list[TextChunk]

    def as_response(self) -> dict[str, Any]:
        return {
            "parserVersion": self.parser_version,
            "chunks": [
                {
                    "chunkIndex": chunk.chunk_index,
                    "content": chunk.content,
                    "tokenCount": chunk.token_count,
                    "tokenCountEstimated": chunk.token_count_estimated,
                    "metadata": chunk.metadata,
                }
                for chunk in self.chunks
            ],
        }


def parse_document(
    file_name: str,
    data: bytes,
    *,
    max_bytes: int,
    max_chars: int,
    overlap_chars: int,
) -> ParsedDocument:
    file_type = _extension(file_name)

    if not data:
        raise DocumentParseError("文件内容为空")
    if len(data) > max_bytes:
        raise DocumentPayloadTooLarge(f"文件超过大小上限（{max_bytes} 字节）")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentParseError("文件必须是 UTF-8 编码的文本") from exc

    if "\x00" in text:
        raise DocumentParseError("文件必须是 UTF-8 编码的文本，不能包含 NUL 字节")

    # 统一换行，避免 CRLF 影响段落边界与重叠长度。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise DocumentParseError("文件内容为空")

    chunks = chunk_text(
        normalized,
        file_type=file_type,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )
    if not chunks:
        raise DocumentParseError("文档没有可索引的文本内容")

    return ParsedDocument(parser_version=PARSER_VERSION, chunks=chunks)


def _extension(file_name: str | None) -> str:
    if file_name is None or not file_name.strip():
        raise DocumentParseError("文件名不能为空")

    # PurePath 在 Windows 上同时识别 / 与 \；先取最后一段，避免路径参与判断。
    name = PurePath(file_name.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise DocumentParseError("文件名不合法")

    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        raise DocumentParseError("仅支持 .md 和 .txt 文件")

    extension = name[dot + 1 :].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise DocumentParseError("仅支持 .md 和 .txt 文件")
    return extension
