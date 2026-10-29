"""流式来源标注过滤。

`validate_citations` 只能**事后**判断一条引用是否合法；等它跑完，回答早已
逐段推给用户，非法引用收不回来。本模块把同一个判定提前到每个 delta：

- 合法来源（本轮真正进入 system instruction 的 `资料《名》#序号`）原样放行；
- 不在本轮合法集合里的精确标注、以及 `[资料…]` / `[#1]` 这类残缺标注直接丢弃；
- 普通方括号（Markdown 链接、代码片段）原样放行。

跨 delta 的标注由 `_pending` 缓冲处理，不是对单块做字符串匹配：
`[资料《or` + `bit.md》#0]` 会先被扣住，拼完整后再判定。只有「可能是标注开头」
的尾巴会被扣住（`[`、`[资`、`[资料`、`[#`），其余文本立即放行，因此正常
流式延迟只增加一个标注 token 的宽度。

本模块不做语义判断：它只处理可机器核对的来源标签，不会改写「模型说没有
检索到资料」这类自然语言断言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.citations import CITATION_LIKE_PATTERN, CITATION_PATTERN

# 一个未闭合的 `[` 最多扣住多久：超过这个长度仍然没有 `]`，就不可能是
# 合法标注（文件名来自数据库，远短于此），按普通文本放行，避免无限缓冲。
MAX_PENDING_CHARS = 512


@dataclass
class CitationFilterReport:
    """一次流式过滤的结果。`dropped` 是精确但不在合法集合里的标注。"""

    provided_labels: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)

    @property
    def removed(self) -> int:
        return len(self.dropped) + len(self.malformed)

    def as_dict(self) -> dict[str, object]:
        return {
            "providedLabels": self.provided_labels,
            "droppedLabels": self.dropped,
            "malformedLabels": self.malformed,
            "removed": self.removed,
        }


class StreamingCitationFilter:
    """按本轮合法来源集合过滤流式文本。

    `allowed_labels` 必须来自**当前请求**的
    `provided_labels_from_context(context, state.knowledge)`，不能复用其它请求、
    用户或 Space 的结果；调用方每次 `generate()` 都新建一个实例。
    """

    def __init__(self, allowed_labels: Iterable[str]) -> None:
        self._allowed = {
            label.strip().strip("[]")
            for label in allowed_labels
            if label and label.strip()
        }
        self._pending = ""
        self.report = CitationFilterReport(provided_labels=sorted(self._allowed))

    def feed(self, delta: str) -> str:
        """吞入一个增量，返回可以安全交付的文本（可能为空）。"""
        if not delta:
            return ""
        self._pending += delta
        return self._drain(final=False)

    def finish(self) -> str:
        """流结束时调用：处理残留缓冲区。"""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        out: list[str] = []
        buf = self._pending
        index = 0
        length = len(buf)

        while index < length:
            opening = buf.find("[", index)
            if opening < 0:
                out.append(buf[index:])
                index = length
                break
            if opening > index:
                out.append(buf[index:opening])

            closing = buf.find("]", opening + 1)
            if closing < 0:
                index = opening
                break

            token = buf[opening : closing + 1]
            if CITATION_PATTERN.fullmatch(token):
                label = token[1:-1].strip()
                if label in self._allowed:
                    out.append(token)
                else:
                    self.report.dropped.append(token)
            elif CITATION_LIKE_PATTERN.fullmatch(token):
                # `[资料…]`、`[#1]` 这类残缺标注不是合法来源，不能交付。
                self.report.malformed.append(token)
            else:
                # Markdown 链接、代码索引等普通方括号：不是标注，原样放行。
                out.append(token)
            index = closing + 1

        self._pending = buf[index:]

        if not self._pending:
            return "".join(out)

        if final:
            if _looks_like_citation_prefix(self._pending):
                self.report.malformed.append(self._pending)
            else:
                out.append(self._pending)
            self._pending = ""
            return "".join(out)

        # 流未结束：只有当尾巴可能是标注开头时才扣住，否则立即放行。
        if not _could_be_citation_prefix(self._pending):
            out.append(self._pending)
            self._pending = ""
        elif len(self._pending) > MAX_PENDING_CHARS:
            # 超长仍未闭合：合法标注不可能这么长，按残缺标注丢弃，
            # 避免把一段可能构成非法来源的文本原样交付。
            self.report.malformed.append(self._pending)
            self._pending = ""
        return "".join(out)


def filter_text(text: str, allowed_labels: Iterable[str]) -> tuple[str, CitationFilterReport]:
    """一次性过滤完整文本（复盘回答等已经缓冲的场景）。"""
    citation_filter = StreamingCitationFilter(allowed_labels)
    cleaned = citation_filter.feed(text) + citation_filter.finish()
    return cleaned, citation_filter.report


def _could_be_citation_prefix(text: str) -> bool:
    """判断未闭合的 `[...` 是否还可能变成一条来源标注。"""
    if not text.startswith("["):
        return False
    if len(text) == 1:
        return True
    rest = text[1:]
    if rest.startswith("资料"):
        return True
    if "资料".startswith(rest):
        return True
    if rest.startswith("#"):
        return len(rest) == 1 or rest[1].isdigit()
    return False


def _looks_like_citation_prefix(text: str) -> bool:
    """流结束时残留的未闭合标注：按残缺标注处理，不交付。"""
    if text.startswith("[资料"):
        return True
    return re.fullmatch(r"\[#\d*", text) is not None
