"""概念识别 —— 判断「用户这句话在说哪些 Concept」。

**这是规则版，不是语义理解。** 后端 `LearningAnalysisService` 的 `rules-v1` 分析器
也是这个路子：按名称匹配文本。之所以先这么做，是因为它：
1. 零成本、可预测、可单测；
2. 不会瞎猜 —— 宁可漏召回，也不误召回一个不相干的概念（与后端同一条原则）。

真正的语义识别（含跨语言、指代消解、「用户其实在问什么」）留给 LLM：
把 `detect_concepts` 换成一次结构化输出调用即可，接口不变。

一处比后端更聪明的地方：**用户消息没命中时回看最近的对话**。
架构文档第四十三节提到，异步学习分析可能还没跑完，下一条消息就来了，
所以「当前对话」的优先级高于「持久化的 Understanding」。用户问「那 TLB Miss 呢？」
时，本轮消息里没有 Concept 名称，但从上一轮能找回 TLB。

回看时**先看用户说过的话，再看 AI 说过的话**：AI 的回答里几乎必然会提到概念名，
如果两者同等对待，「用户到底在学什么」就会被 AI 自己的措辞带偏。
"""

from __future__ import annotations

import re

from app.schemas import Concept, HistoryMessage

# 名称别名表：键是 canonical_name 的小写形式。
# 只收录「同一概念的不同说法」，不做同义词推理 —— 那属于模型该干的事。
ALIASES: dict[str, tuple[str, ...]] = {
    "tlb": ("快表", "转换检测缓冲区", "translation lookaside buffer"),
    "page table": ("页表", "分页表"),
    "page fault": ("缺页", "缺页异常", "缺页中断"),
    "virtual memory": ("虚拟内存", "虚拟存储", "虚存"),
    "tcp": ("传输控制协议",),
    "redis": ("缓存中间件",),
}

# 用户消息里命中：最高置信
_SCORE_IN_MESSAGE = 1.0
# 别名命中：稍低，因为别名表是人写的
_SCORE_ALIAS = 0.9
# 从历史回看时，每往回一条衰减一点
_SCORE_HISTORY_BASE = 0.6
_HISTORY_DECAY = 0.1


def detect_concepts(
    user_message: str,
    history: list[HistoryMessage],
    concepts: list[Concept],
    *,
    history_lookback: int = 4,
) -> list[tuple[Concept, float, str]]:
    """返回 `(concept, score, matched_on)`，按 score 降序。

    `history` 按时间正序传入（最后一个元素是最近一条）。
    """
    if not concepts:
        return []

    current = user_message or ""
    hits: dict[int, tuple[Concept, float, str]] = {}

    for concept in concepts:
        matched_on = _match_concept(concept, current)
        if matched_on is not None:
            score = _SCORE_IN_MESSAGE if matched_on == "canonical_name" else _SCORE_ALIAS
            hits[concept.id] = (concept, score, f"message:{matched_on}")

    if hits:
        return sorted(hits.values(), key=lambda item: item[1], reverse=True)

    # 本轮没命中 → 回看最近几轮对话，越近权重越高
    recent = list(reversed(history[-history_lookback:])) if history_lookback > 0 else []
    user_turns = [(distance, message.content) for distance, message in enumerate(recent) if message.is_user()]
    assistant_turns = [
        (distance, message.content) for distance, message in enumerate(recent) if not message.is_user()
    ]

    for group in (user_turns, assistant_turns):
        found: dict[int, tuple[Concept, float, str]] = {}
        for distance, text in group:
            for concept in concepts:
                if concept.id in found:
                    continue
                matched_on = _match_concept(concept, text or "")
                if matched_on is None:
                    continue
                score = max(0.0, _SCORE_HISTORY_BASE - distance * _HISTORY_DECAY)
                found[concept.id] = (concept, score, f"history-{distance + 1}:{matched_on}")
        if found:
            hits = found
            break

    return sorted(hits.values(), key=lambda item: item[1], reverse=True)


def _match_concept(concept: Concept, text: str) -> str | None:
    """命中返回依据，没命中返回 None。"""
    if not text:
        return None
    if _contains(text, concept.canonical_name):
        return "canonical_name"
    if concept.display_name and _contains(text, concept.display_name):
        return "display_name"
    for alias in ALIASES.get(concept.canonical_name.strip().lower(), ()):
        if _contains(text, alias):
            return f"alias:{alias}"
    return None


def _contains(text: str, term: str) -> bool:
    """子串匹配。

    纯 ASCII 的短名称（TLB / TCP / Redis）要求词边界，否则 `TLB` 会被
    `TLBSize` 之类的词误命中；中文没有词边界，直接子串匹配。
    """
    term = term.strip()
    if not term:
        return False
    if term.isascii() and term.replace(" ", "").isalnum():
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return term in text
