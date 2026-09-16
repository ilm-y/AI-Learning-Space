"""概念识别的行为约定。

这一层替换率最高（将来会换成 LLM 语义识别），所以把它的**边界**钉死：
命中什么、不命中什么、什么时候回看历史。换实现时这些断言仍然应该成立。
"""

from __future__ import annotations

from app.retriever.concept_detector import detect_concepts
from app.schemas import Concept, HistoryMessage, MessageRole

CONCEPTS = [
    Concept(id=10, canonical_name="TLB", display_name="TLB（快表）"),
    Concept(id=11, canonical_name="Page Table", display_name="页表"),
    Concept(id=12, canonical_name="Page Fault", display_name="缺页异常"),
    Concept(id=13, canonical_name="Virtual Memory", display_name="虚拟内存"),
]


def _ids(detected) -> list[int]:
    return [concept.id for concept, _, _ in detected]


def _user_turns(*texts: str) -> list[HistoryMessage]:
    return [HistoryMessage(role=MessageRole.USER, content=text) for text in texts]


def test_matches_canonical_name_case_insensitively():
    assert _ids(detect_concepts("tlb 是什么？", [], CONCEPTS)) == [10]


def test_matches_chinese_alias():
    detected = detect_concepts("快表是干什么的？", [], CONCEPTS)

    assert _ids(detected) == [10]
    assert detected[0][2].startswith("message:alias:")


def test_ascii_name_requires_word_boundary():
    """`TLBSize` 不该被当成提到了 TLB —— 否则误召回会污染整段上下文。"""
    assert detect_concepts("TLBSize 这个字段怎么算？", [], CONCEPTS) == []


def test_returns_multiple_concepts_ordered_by_score():
    detected = detect_concepts("TLB Miss 和 Page Fault 是什么关系？", [], CONCEPTS)

    assert set(_ids(detected)) == {10, 12}
    assert [score for _, score, _ in detected] == sorted(
        (score for _, score, _ in detected), reverse=True
    )


def test_falls_back_to_recent_history():
    """用户说「那它未命中之后呢？」时本轮没有概念名，但上一轮有 TLB。"""
    detected = detect_concepts("那它未命中之后呢？", _user_turns("TLB 是什么？"), CONCEPTS)

    assert _ids(detected) == [10]
    assert detected[0][2].startswith("history-1:")


def test_history_fallback_decays_with_distance():
    detected = detect_concepts("接着说", _user_turns("TLB 是什么？", "先看别的", "嗯"), CONCEPTS)

    assert _ids(detected) == [10]
    assert detected[0][1] < 0.6


def test_user_turns_win_over_assistant_turns():
    """AI 自己的回答里必然满是概念名 —— 不能让它的措辞决定「用户在学什么」。"""
    history = [
        HistoryMessage(role=MessageRole.USER, content="虚拟内存是怎么实现的？"),
        HistoryMessage(role=MessageRole.ASSISTANT, content="这要从 TLB 和 Page Table 讲起。"),
    ]

    assert _ids(detect_concepts("接着说", history, CONCEPTS)) == [13]


def test_assistant_turns_are_used_when_the_user_said_nothing_relevant():
    history = [
        HistoryMessage(role=MessageRole.USER, content="继续"),
        HistoryMessage(role=MessageRole.ASSISTANT, content="我们刚才在讲 Page Fault。"),
    ]

    assert _ids(detect_concepts("嗯", history, CONCEPTS)) == [12]


def test_does_not_guess_when_nothing_matches():
    """宁可漏召回，也不瞎召回 —— 与后端规则版分析器同一条原则。"""
    assert detect_concepts("今天天气怎么样", _user_turns("随便聊聊"), CONCEPTS) == []


def test_empty_concept_list_is_safe():
    assert detect_concepts("TLB 是什么", [], []) == []
