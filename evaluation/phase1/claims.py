"""把「掌握程度断言」拆成可核对的结构化事实。

为什么需要它：只对回答做 `"已经完全掌握" not in text` 的关键词否定，会把
「现在还不能说你已经完全掌握」误判成肯定。语义方向由**所在从句**决定：

- 肯定：`你已经完全掌握。`
- 否定：`现在还不能说你已经完全掌握。`
- 历史：`历史上曾判断为完全掌握，但当时记录有限。`
- 问句：`你现在已经完全掌握了吗？`

这里只处理「完全 / 彻底 / 全部 + 掌握 / 理解」这类**绝对**表述。
「掌握了核心概念，但仍需验证边界」没有绝对化，不算绝对掌握断言。
这不是通用 NLP，也不替代人工复核：它只把方向、时间范围和否定上下文
显式列出来，供硬断言与报告核对。
"""

from __future__ import annotations

import re
from typing import Any

# 绝对化掌握表述。单独的「掌握 / 理解」不在此列：限定范围的「掌握了核心概念」
# 是允许的，不能和「完全掌握」混为一谈。
ABSOLUTE_MASTERY_RE = re.compile(
    r"(?:完全|彻底|全面|全部|百分百|100%)\s*(?:地)?\s*(?:掌握|理解|弄懂|弄明白)"
)
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;。！？!?——\n]")
# 否定 / 证据不足。范围刻意宽一点：宁可把「并非完全掌握」记为否定，
# 也不要把它误判成肯定。是否真的构成否定仍由人工复核抽查。
_NEGATION_RE = re.compile(
    r"不|未|没|无|非|别|尚未|未能|无法|不足以|不够|谈不上|称不上|"
    r"证据不足|尚缺|还缺|还差|尚未确认|不能确认|无法确认|不能断言"
)
_HISTORICAL_RE = re.compile(
    r"曾|曾经|历史上|此前|过去|以前|当时|一度|原先|原来|早前|较早"
)
_CURRENT_RE = re.compile(r"现在|目前|当前|此刻|如今|本轮|这轮|本次")
_QUESTION_RE = re.compile(r"[？?]\s*$|吗\s*[。！!]?\s*$|是否|有没有")


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = 0
    right = len(text)
    for delimiter in _CLAUSE_SPLIT_RE.finditer(text):
        if delimiter.end() <= start:
            left = delimiter.end()
        elif delimiter.start() >= end:
            right = delimiter.start()
            break
    return left, right


def _direction(clause: str) -> str:
    if _QUESTION_RE.search(clause):
        return "question"
    if _NEGATION_RE.search(clause):
        return "negated"
    if _HISTORICAL_RE.search(clause) and not _CURRENT_RE.search(clause):
        return "historical"
    return "affirmative"


def analyze_mastery_claim(answer: str) -> dict[str, Any]:
    """返回绝对掌握表述及其方向；没有绝对表述时 `affirmativeCurrent=False`。"""
    text = answer or ""
    details: list[dict[str, str]] = []
    for match in ABSOLUTE_MASTERY_RE.finditer(text):
        left, right = _clause_bounds(text, match.start(), match.end())
        clause = text[left:right].strip()
        details.append(
            {
                "phrase": match.group(0),
                "clause": clause,
                "direction": _direction(clause),
            }
        )
    counts = {
        direction: sum(1 for item in details if item["direction"] == direction)
        for direction in ("affirmative", "negated", "historical", "question")
    }
    return {
        "phrases": [item["phrase"] for item in details],
        "details": details,
        "affirmativeCount": counts["affirmative"],
        "negatedCount": counts["negated"],
        "historicalCount": counts["historical"],
        "questionCount": counts["question"],
        # 只有「当前、非否定、非问句」的绝对表述才算肯定断言。
        "affirmativeCurrent": counts["affirmative"] > 0,
        "hasAbsolutePhrase": bool(details),
        "note": (
            "方向由绝对表述所在从句判定；历史/否定/问句不计为当前肯定。"
            "这不是事实正确性证明，边界样本需人工复核。"
        ),
    }
