"""生成 Phase 3.2 盲评版人工复核表。

读取 ``human-review-queue-20261028.json``，保留回答全文、上下文事实摘要、
完成状态与 0/1/2 锚点，**移除自动 Judge 分数、自动 case_status、判读理由与
复核原因**，避免评分者被自动结论锚定。

原始评分表 ``phase3.1-human-review-sheet.md`` 保持不变，供完成盲评后对照；
本脚本不调用模型、不填写任何人工分数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.phase3.build_human_review import (
    EXAMPLES,
    METRIC_LABELS,
    METRIC_ORDER,
    REPO_ROOT,
    context_summary,
)

QUEUE_PATH = REPO_ROOT / "evaluation/phase3/reports/human-review-queue-20261028.json"
OUT_MD = REPO_ROOT / "docs/testing/phase3.2-human-review-blind.md"


def _blind_item(item: dict[str, Any]) -> dict[str, Any]:
    summary = item["contextSummary"]
    if "Evidence：" not in summary:
        # context_summary 只在 evidenceIds 非空时输出该行；缺失即该轮没有按需 Evidence。
        summary = summary.rstrip() + "\n- Evidence：0 条（该轮没有按需 Evidence）"
    return {
        "caseId": item["caseId"],
        "runIndex": item["runIndex"],
        "artifact": item["artifact"],
        "userMessage": item["userMessage"],
        "completionStatus": item["completionStatus"],
        "finishReason": item["finishReason"],
        "contextSummary": summary,
        "answerText": item["answerText"],
        "rubric": item["rubric"],
    }


def render(items: list[dict[str, Any]]) -> str:
    out: list[str] = [
        "# Phase 3.2 人工复核评分表（盲评版）",
        "",
        "生成日期：2026-10-28。",
        "",
        "本表对应 Phase 3 的 9 条待复核记录。**当前人工评分为 0/9。**",
        "",
        "与 [Phase 3.1 评分表](phase3.1-human-review-sheet.md) 的区别：本表**不显示**"
        "自动 Judge 分数、自动 `case_status`、自动判读理由与复核原因，只保留评分所需的"
        "回答全文、上下文事实、完成状态和 rubric。目的是避免评分者被自动结论锚定。",
        "",
        "建议流程：先只使用本表独立完成 9 条评分；填写完成后再打开 Phase 3.1 原表"
        "对照自动分，并单独记录一致/不一致。**在人工分数全部填写前，不计算"
        "Judge–human 一致率、Kappa 或“Judge 已校准”的结论。**",
        "",
        "## 填写说明",
        "",
        "- 分数：`0` 不满足、`1` 部分满足、`2` 满足；该维度不适用时填 `null`。",
        "- 每个维度必须写一句理由，引用回答中的具体句子或上下文事实。",
        "- 只根据回答全文和给定上下文评分；不要根据用例编号、完成状态或模型自我评价倒推。",
        "- 截断回答（`TRUNCATED`）的 `task_completion` 最高记 1：内容可能正确，但任务未完成。",
        "- 人工判定填 `PASS` / `FAIL` / `NEEDS-THIRD-REVIEW`，与自动分无关。",
        "",
        "## 评分维度与示例",
        "",
        "示例只说明锚点，不是这 9 条样本的答案。",
        "",
    ]
    for metric in METRIC_ORDER:
        good, bad = EXAMPLES[metric]
        out.extend(
            [
                f"### {METRIC_LABELS[metric]}（`{metric}`）",
                "",
                f"- 满意示例：{good}",
                f"- 不满意示例：{bad}",
                "",
            ]
        )

    for index, item in enumerate(items, start=1):
        out.extend(
            [
                f"## {index}. {item['caseId']}-r{item['runIndex']}",
                "",
                f"- artifact：`{item['artifact']}`",
                f"- completion_status：**{item['completionStatus']}**"
                f"（finish_reason={item['finishReason']}）",
                f"- 用户输入：{item['userMessage']}",
                "",
                "### 上下文事实摘要",
                "",
                item["contextSummary"],
                "",
                "### 原始回答（未修改，可能被截断）",
                "",
                "````text",
                item["answerText"],
                "````",
                "",
                "### 人工评分（待填写）",
                "",
                "| 维度 | 0 | 1 | 2 | 分数 | 理由 | 备注 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for metric in METRIC_ORDER:
            rubric = item["rubric"].get(metric, {})
            out.append(
                "| {label} | {s0} | {s1} | {s2} |  |  |  |".format(
                    label=METRIC_LABELS[metric],
                    s0=str(rubric.get("score0", "")).replace("|", "／"),
                    s1=str(rubric.get("score1", "")).replace("|", "／"),
                    s2=str(rubric.get("score2", "")).replace("|", "／"),
                )
            )
        out.extend(
            [
                "",
                "- 人工判定：`PASS` / `FAIL` / `NEEDS-THIRD-REVIEW`（待填）",
                "- 总体理由：（待填）",
                "- 评分人 / 日期：（待填）",
                "",
            ]
        )

    out.extend(
        [
            "## 统计口径",
            "",
            f"- 待复核记录：{len(items)} 条。",
            "- 已完成人工评分：0 条。",
            f"- 待填写：{len(items)} 条。",
            "",
            "9 条只是初步样本，分布缺少变化；即使全部填写，也不足以用加权 Kappa "
            "证明 Judge 已校准。结论应表述为“有限样本上的一致性观察”。",
            "",
        ]
    )
    return "\n".join(out)


def main() -> None:
    payload = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    items = [_blind_item(item) for item in payload["items"]]
    OUT_MD.write_text(render(items), encoding="utf-8")
    print(f"wrote {OUT_MD.relative_to(REPO_ROOT)} ({len(items)} items)")


if __name__ == "__main__":
    main()
