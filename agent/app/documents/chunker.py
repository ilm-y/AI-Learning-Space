"""纯文本 / Markdown 切块。

设计目标是「可预测、可测试」，而不是追求复杂 NLP：

* Markdown 按标题层级维护 heading path，正文按空行分段；
* 代码围栏整体保留，绝不在围栏内部切开；
* 相邻短段落尽可能合并到一个块，直到 `max_chars`；
* 单个超长段落在换行 / 中英文句末 / 空白处切分，并保留 `overlap_chars` 重叠；
* 切块索引由最终输出顺序决定，从 0 连续递增，重复调用结果完全一致。

所有长度都按 Python 字符数计算。token_count 是明确的估算值（约 4 字符 / token），
不假装是模型 tokenizer 的精确结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN_CHARS = 4
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SENTENCE_BREAKS = ("\n", "。", "！", "？", ".", "!", "?", "；", ";", "，")


@dataclass(frozen=True)
class TextChunk:
    """切块结果，chunk_index 在 `chunk_blocks` 返回前统一赋值。"""

    content: str
    chunk_index: int
    token_count: int
    token_count_estimated: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Block:
    text: str
    heading_path: tuple[str, ...]
    is_code: bool


def estimate_tokens(text: str) -> int:
    """轻量估算；结果在 metadata 中明确标记为 estimated。"""
    return max(1, (len(text) + _TOKEN_CHARS - 1) // _TOKEN_CHARS)


def chunk_text(
    text: str,
    *,
    file_type: str,
    max_chars: int = 1200,
    overlap_chars: int = 150,
) -> list[TextChunk]:
    """把已规范化的文本切成稳定块。

    `file_type` 只影响 Markdown 标题解析；未知类型按纯文本处理。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    if overlap_chars < 0:
        raise ValueError("overlap_chars 不能为负数")
    overlap = min(overlap_chars, max_chars - 1)

    normalized = text.strip()
    if not normalized:
        return []

    if file_type.lower() == "md":
        blocks = _markdown_blocks(normalized)
    else:
        blocks = _plain_blocks(normalized)

    raw_chunks = _merge_blocks(blocks, max_chars=max_chars, overlap=overlap)
    chunks: list[TextChunk] = []
    for content, heading_path, is_code in raw_chunks:
        final_content = _with_heading_path(content, heading_path)
        metadata: dict[str, Any] = {
            "headingPath": list(heading_path),
            "fileType": file_type.lower(),
            "contentType": "code" if is_code else "text",
            "charCount": len(final_content),
            "tokenCountEstimated": True,
        }
        chunks.append(
            TextChunk(
                content=final_content,
                chunk_index=len(chunks),
                token_count=estimate_tokens(final_content),
                token_count_estimated=True,
                metadata=metadata,
            )
        )
    return chunks


# ===================== block 解析 =====================


def _markdown_blocks(text: str) -> list[_Block]:
    lines = text.split("\n")
    blocks: list[_Block] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph: list[str] = []

    in_code = False
    fence_char = ""
    fence_len = 0
    code_lines: list[str] = []
    code_heading_path: tuple[str, ...] = ()

    def current_path() -> tuple[str, ...]:
        return tuple(title for _, title in heading_stack)

    def flush_paragraph() -> None:
        if not paragraph:
            return
        content = "\n".join(paragraph).strip()
        paragraph.clear()
        if content:
            blocks.append(_Block(content, current_path(), False))

    for line in lines:
        if in_code:
            code_lines.append(line)
            if _is_closing_fence(line, fence_char, fence_len):
                blocks.append(_Block("\n".join(code_lines).strip(), code_heading_path, True))
                code_lines = []
                in_code = False
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            in_code = True
            fence_char = fence.group(1)[0]
            fence_len = len(fence.group(1))
            code_lines = [line]
            code_heading_path = current_path()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            continue

        if not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line)

    # 未闭合的代码围栏按代码块处理，宁可保留内容也不丢正文。
    if in_code and code_lines:
        blocks.append(_Block("\n".join(code_lines).strip(), code_heading_path, True))
    flush_paragraph()
    return [block for block in blocks if block.text.strip()]


def _plain_blocks(text: str) -> list[_Block]:
    paragraphs = re.split(r"\n[ \t]*\n", text)
    return [
        _Block(paragraph.strip(), (), False)
        for paragraph in paragraphs
        if paragraph.strip()
    ]


def _is_closing_fence(line: str, fence_char: str, fence_len: int) -> bool:
    stripped = line.strip()
    if len(stripped) < fence_len:
        return False
    if any(char != fence_char for char in stripped):
        return False
    return True


# ===================== 合并与切分 =====================


def _merge_blocks(
    blocks: list[_Block], *, max_chars: int, overlap: int
) -> list[tuple[str, tuple[str, ...], bool]]:
    result: list[tuple[str, tuple[str, ...], bool]] = []
    pending = ""
    pending_path: tuple[str, ...] = ()

    def flush_pending() -> None:
        nonlocal pending
        if pending.strip():
            result.append((pending.strip(), pending_path, False))
        pending = ""

    for block in blocks:
        if block.is_code:
            # 代码块独立成块，不与其他正文合并，也不在内部切分。
            flush_pending()
            result.append((block.text, block.heading_path, True))
            continue

        if pending and block.heading_path != pending_path:
            flush_pending()

        candidate = f"{pending}\n\n{block.text}" if pending else block.text
        if len(candidate) <= max_chars:
            pending = candidate
            pending_path = block.heading_path
            continue

        flush_pending()
        if len(block.text) <= max_chars:
            pending = block.text
            pending_path = block.heading_path
            continue

        for piece in _split_oversized(block.text, max_chars=max_chars, overlap=overlap):
            result.append((piece, block.heading_path, False))

    flush_pending()
    return result


def _split_oversized(text: str, *, max_chars: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            window = text[start:end]
            half = max(1, len(window) // 2)
            cut = max(window.rfind(separator) for separator in _SENTENCE_BREAKS)
            if cut >= half:
                end = start + cut + 1
            else:
                whitespace = max(window.rfind(" "), window.rfind("\t"))
                if whitespace >= half:
                    end = start + whitespace + 1

        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= length:
            break

        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    return pieces


def _with_heading_path(content: str, heading_path: tuple[str, ...]) -> str:
    if not heading_path:
        return content
    return f"{' > '.join(heading_path)}\n\n{content}"
