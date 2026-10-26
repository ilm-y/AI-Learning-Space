"""Spring SSE 验收判定器的离线测试。

S04 的「承认资料不足」原先只要回答里出现「没有 / 无法」就通过，会把
「没有直接答案，但……（然后编造）」也放行。这里锁定 v2 的精确语义：
必须是否定词 + 所问对象；引用仍然必须为空；SSE 事件契约不变。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation.rag.spring_sse import analyze_events, judge  # noqa: E402

S04 = {
    "id": "S04",
    "question": "请解释量子色动力学中的渐进自由。",
    "requiredTerms": [],
    "requiredCitationDocuments": [],
    "expectNoCitation": True,
    "expectAdmission": True,
    "admissionPatterns": [
        r"(?:资料|文档|上传)[^。！？\n]{0,15}(?:没有|未|不)(?:直接|相关|命中|检索到|找到|包含|提供|提及|覆盖)",
        r"(?:没有|未)(?:找到|检索到|命中|检索出)[^。！？\n]{0,15}(?:资料|文档|内容|相关|依据)",
        r"没有直接答案",
    ],
}


def _events(answer: str) -> list[tuple[str, dict]]:
    return [
        ("message.start", {"runId": "r1", "conversationId": "c1"}),
        ("message.delta", {"delta": answer}),
        ("message.end", {"messageId": "m1", "runId": "r1"}),
    ]


def _checks(answer: str) -> dict[str, dict]:
    return {check["name"]: check for check in judge(S04, analyze_events(_events(answer)))}


def test_precise_admission_passes_without_citation():
    checks = _checks(
        "本轮上传资料中没有检索到可确认的依据。以下仅是通用知识：渐进自由指……"
    )

    assert checks["admits_missing_document"]["passed"] is True
    assert checks["no_fabricated_citation"]["passed"] is True
    assert all(check["passed"] for check in checks.values())


def test_bare_negation_or_contradiction_does_not_count_as_admission():
    checks = _checks("虽然没有资料直接说明，但渐进自由就是夸克越近越自由。")
    assert checks["admits_missing_document"]["passed"] is False

    # 承认不足后又给出通用解释，但没有明确「资料中没有可确认依据」：仍不通过。
    checks = _checks("没有资料，不过渐进自由是指强相互作用在高能下变弱。")
    assert checks["admits_missing_document"]["passed"] is False


def test_admission_with_fabricated_citation_still_fails():
    checks = _checks(
        "资料中没有检索到相关依据 [资料《zenframe.md》#0]，但通用知识认为……"
    )

    assert checks["admits_missing_document"]["passed"] is True
    assert checks["no_fabricated_citation"]["passed"] is False
    assert checks["no_malformed_citations"]["passed"] is True


def test_sse_event_contract_is_still_enforced():
    checks = _checks("本轮上传资料中没有检索到可确认的依据。")

    assert checks["sse_format"]["passed"] is True
    assert checks["start_first"]["passed"] is True
    assert checks["end_last"]["passed"] is True
    assert checks["delta_fields"]["passed"] is True
