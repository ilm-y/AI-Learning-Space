"""文档解析 / 切块的离线单元测试。"""

from __future__ import annotations

import pytest

from app.documents.errors import DocumentParseError, DocumentPayloadTooLarge
from app.documents.parser import parse_document


def parse(data: bytes, name: str = "notes.md", *, max_chars: int = 1200, overlap: int = 150):
    return parse_document(
        name,
        data,
        max_bytes=1024 * 1024,
        max_chars=max_chars,
        overlap_chars=overlap,
    )


def test_bom_and_crlf_are_normalized() -> None:
    parsed = parse("﻿# 标题\r\n\r\n第一段\r\n第二行\r\n".encode("utf-8"))

    assert parsed.parser_version == "document-parser-v1"
    assert len(parsed.chunks) == 1
    content = parsed.chunks[0].content
    assert "\r" not in content
    assert "第一段\n第二行" in content
    assert parsed.chunks[0].metadata["headingPath"] == ["标题"]
    assert parsed.chunks[0].token_count_estimated is True


def test_txt_uses_paragraph_boundaries() -> None:
    parsed = parse(b"first paragraph\n\nsecond paragraph", "notes.txt", max_chars=1000)

    assert len(parsed.chunks) == 1
    assert "first paragraph\n\nsecond paragraph" == parsed.chunks[0].content


def test_markdown_keeps_nested_heading_path() -> None:
    text = "# A\n\nintro\n\n## B\n\ndetail\n\n### C\n\ndeep"
    parsed = parse(text.encode("utf-8"), max_chars=1000)

    paths = [chunk.metadata["headingPath"] for chunk in parsed.chunks]
    assert ["A"] in paths
    assert ["A", "B"] in paths
    assert ["A", "B", "C"] in paths
    for chunk in parsed.chunks:
        if chunk.metadata["headingPath"]:
            prefix = " > ".join(chunk.metadata["headingPath"])
            assert chunk.content.startswith(prefix + "\n\n")


def test_code_fence_is_never_split() -> None:
    fence = "```python\n" + "\n".join(f"print({i})" for i in range(80)) + "\n```"
    text = f"# Code\n\nbefore\n\n{fence}\n\nafter"

    parsed = parse(text.encode("utf-8"), max_chars=50, overlap=10)

    code_chunks = [
        chunk for chunk in parsed.chunks if chunk.metadata["contentType"] == "code"
    ]
    assert len(code_chunks) == 1
    assert code_chunks[0].content.count("```") == 2
    assert "print(79)" in code_chunks[0].content
    assert len(code_chunks[0].content) > 50


def test_oversized_paragraph_splits_with_overlap_and_stable_indexes() -> None:
    text = "".join(chr(ord("a") + (i % 26)) for i in range(500))
    first = parse(text.encode("utf-8"), "notes.txt", max_chars=120, overlap=25)
    second = parse(text.encode("utf-8"), "notes.txt", max_chars=120, overlap=25)

    assert len(first.chunks) > 1
    assert [c.chunk_index for c in first.chunks] == list(range(len(first.chunks)))
    assert [c.content for c in first.chunks] == [c.content for c in second.chunks]
    for chunk in first.chunks:
        assert len(chunk.content) <= 120
    assert first.chunks[1].content.startswith(first.chunks[0].content[-25:])


def test_invalid_extension_is_rejected() -> None:
    with pytest.raises(DocumentParseError, match="仅支持"):
        parse(b"content", "notes.pdf")


def test_missing_extension_is_rejected() -> None:
    with pytest.raises(DocumentParseError, match="仅支持"):
        parse(b"content", "notes")


def test_empty_document_is_rejected() -> None:
    with pytest.raises(DocumentParseError, match="文件内容为空"):
        parse(b"", "notes.md")
    with pytest.raises(DocumentParseError, match="文件内容为空"):
        parse(b"   \n\t\r\n", "notes.md")


def test_invalid_utf8_and_nul_are_rejected() -> None:
    with pytest.raises(DocumentParseError, match="UTF-8"):
        parse(b"\xff\xfe\x00bad", "notes.txt")
    with pytest.raises(DocumentParseError, match="NUL"):
        parse(b"hello\x00world", "notes.txt")


def test_oversized_document_is_rejected() -> None:
    with pytest.raises(DocumentPayloadTooLarge):
        parse_document(
            "notes.txt",
            b"x" * 10,
            max_bytes=5,
            max_chars=100,
            overlap_chars=10,
        )


def test_markdown_only_headings_has_no_indexable_content() -> None:
    with pytest.raises(DocumentParseError, match="可索引"):
        parse(b"# Title\n\n## Subtitle", "notes.md")
