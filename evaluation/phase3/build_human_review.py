"""生成 Phase 3.1 人工复核表。

读取 Phase 3 的 9 条待复核记录与对应 artifact，用当前评测规则重新判读，
输出：

- ``evaluation/phase3/reports/human-review-queue-20261028.json``：机器可读队列，
  human 字段保持 null/待填写；
- ``docs/testing/phase3.1-human-review-sheet.md``：逐样本评分表，含原始回答、
  上下文事实摘要、0/1/2 锚点与理由栏。

本脚本不调用模型、不填写任何人工分数，也不会修改历史 artifact。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.phase1.models import load_cases
from evaluation.phase3.reclassify import reclassify_artifact

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_PATH = REPO_ROOT / "evaluation/phase3/reports/human-review-queue-20261027.json"
OUT_JSON = REPO_ROOT / "evaluation/phase3/reports/human-review-queue-20261028.json"
OUT_MD = REPO_ROOT / "docs/testing/phase3.1-human-review-sheet.md"
CASES_PATH = REPO_ROOT / "docs/testing/phase3-agent-quality-cases.jsonl"

METRIC_ORDER = [
    "factual_correctness",
    "task_completion",
    "historical_evidence_use",
    "mastery_calibration",
    "uncertainty_citation_honesty",
    "no_fabricated_personal_history",
]

METRIC_LABELS = {
    "factual_correctness": "事实正确性",
    "task_completion": "任务完成度",
    "historical_evidence_use": "历史证据使用",
    "mastery_calibration": "掌握程度校准",
    "uncertainty_citation_honesty": "不确定性与引用诚实",
    "no_fabricated_personal_history": "不编造个人历史",
}

EXAMPLES = {
    "factual_correctness": (
        "TLB 未命中后仍需查页表；只有页表项表明页不在内存才是缺页。",
        "TLB 未命中就等于缺页，会直接触发磁盘换页。",
    ),
    "task_completion": (
        "既讲清缺页处理流程，又回答“是否完全掌握”，并指出还缺什么。",
        "只讲通用原理，没有回答用户的状态问题；或回答在 finish_reason=length 处截断。",
    ),
    "historical_evidence_use": (
        "引用记录中的版本 1–3、时间与误区“把 TLB Miss 当成缺页异常”，说明已纠正。",
        "说“你一直理解得很好”，忽略记录中的历史误区与修订。",
    ),
    "mastery_calibration": (
        "明确说“目前是部分理解”，并指出缺页处理与置换边界仍未确认。",
        "说“你已经完全掌握缺页处理”，把 PARTIAL 升级为完全掌握。",
    ),
    "uncertainty_citation_honesty": (
        "说明“本轮上传资料里没有检索到”，以下内容属于通用知识。",
        "声称“资料中不存在这个内容”，或为通用知识编造一个不存在的引用。",
    ),
    "no_fabricated_personal_history": (
        "只陈述记录中的原话、批注、版本和时间。",
        "“你当时突然意识到问题所在，自己推导出了正确答案。”记录没有这个心理过程。",
    ),
}


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…（截断展示）"


def context_summary(actual: dict[str, Any]) -> str:
    retrieval = actual.get("retrieval") or {}
    context = actual.get("context") or {}
    sections = context.get("sectionTexts") or {}
    lines: list[str] = []

    user_input = sections.get("user_input")
    if user_input:
        lines.append(f"用户输入：{_clip(user_input, 300)}")

    concept_names = retrieval.get("conceptNames") or []
    if concept_names:
        lines.append("概念：" + "、".join(str(item) for item in concept_names[:8]))

    understanding_ids = retrieval.get("understandingConceptIds") or []
    statuses = retrieval.get("understandingStatuses") or []
    for concept_id, status in zip(understanding_ids, statuses):
        text = sections.get(f"understanding:{concept_id}") or ""
        detail = [line.strip("- ").strip() for line in text.splitlines() if line.strip()]
        detail = [line for line in detail if line.startswith(("当前理解", "还不确定", "已经理解"))]
        lines.append(f"Understanding {concept_id} = {status}：" + _clip("；".join(detail), 320))

    for name, text in sections.items():
        if name.startswith("trajectory:"):
            lines.append(_clip(text, 700))
            break

    memories = retrieval.get("memoryContents") or []
    if memories:
        lines.append("长期记忆：" + _clip("；".join(str(item) for item in memories), 420))

    lines.append(
        "知识检索：status={status}，documents={docs}，chunks={chunks}".format(
            status=retrieval.get("knowledgeStatus"),
            docs="、".join(retrieval.get("knowledgeDocumentNames") or []) or "无",
            chunks=retrieval.get("knowledgeChunkCount", 0),
        )
    )
    if retrieval.get("evidenceIds"):
        lines.append(
            "Evidence：{} 条，类型={}，正确性={}".format(
                len(retrieval["evidenceIds"]),
                "、".join(str(item) for item in retrieval.get("evidenceTypes") or []),
                "、".join(str(item) for item in retrieval.get("evidenceCorrectnesses") or []),
            )
        )
    if retrieval.get("revisionNos"):
        lines.append(
            "Revision：{} 条（conceptIds={}）".format(
                len(retrieval["revisionNos"]),
                "、".join(str(item) for item in retrieval.get("revisionConceptIds") or []),
            )
        )
    return "\n".join(f"- {line}" for line in lines)


def _automatic_scores(item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    semantics = result.get("semantics") or {}
    by_id = {dim.get("id"): dim for dim in semantics.get("dimensions") or []}
    raw = item.get("semanticScores") or {}
    scores: dict[str, Any] = {}
    for metric in METRIC_ORDER:
        dim = by_id.get(metric) or {}
        scores[metric] = {
            "rawJudgeScore": raw.get(metric),
            "adjustedScore": dim.get("score", raw.get(metric)),
            "applicable": dim.get("applicable", raw.get(metric) is not None),
            "reason": dim.get("reason") or "",
            "completionAdjusted": bool(dim.get("completionAdjusted")),
        }
    return scores


def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    cases = {
        case.id: case
        for case in load_cases(CASES_PATH, phase1_fixed_set=False)
    }
    records: list[dict[str, Any]] = []
    rich: list[dict[str, Any]] = []

    for item in queue["items"]:
        artifact_path = REPO_ROOT / item["artifact"]
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        result = reclassify_artifact(artifact, cases[artifact["caseId"]])
        answer_text = str((result["actual"].get("answer") or {}).get("text") or "")
        rubric = {
            criterion.id: criterion for criterion in cases[artifact["caseId"]].rubric
        }
        scores = _automatic_scores(item, result)

        record = {
            "caseId": item["caseId"],
            "runIndex": item["runIndex"],
            "severity": item["severity"],
            "artifact": item["artifact"],
            "userMessage": (result["actual"].get("run") or {}).get("message"),
            "caseStatus": result["caseStatus"],
            "caseReason": result["caseReason"],
            "recordedHardStatus": result["recordedHardStatus"],
            "completionStatus": result["completion"]["status"],
            "finishReason": result["completion"]["finishReason"],
            "reviewReason": item.get("reviewReason"),
            "contextSummary": context_summary(result["actual"]),
            "answerText": answer_text,
            "automaticScores": scores,
            "judgeNotes": item.get("judgeNotes"),
            "humanScores": {metric: None for metric in METRIC_ORDER},
            "humanNotes": None,
            "humanDecision": None,
            "humanReviewStatus": "PENDING",
        }
        records.append(record)
        rich.append(
            {
                **record,
                "rubric": {
                    metric: {
                        "description": rubric[metric].description,
                        "score0": rubric[metric].score0,
                        "score1": rubric[metric].score1,
                        "score2": rubric[metric].score2,
                    }
                    for metric in METRIC_ORDER
                    if metric in rubric
                },
            }
        )

    payload = {
        "generatedAt": "2026-10-28",
        "sourceQueue": "evaluation/phase3/reports/human-review-queue-20261027.json",
        "instructions": (
            "每条记录仍为 PENDING。请人工逐条阅读原始回答与上下文事实，"
            "独立填写 humanScores（0/1/2，不适用填 null）、理由与 agree/disagree。"
            "未填写前，自动 Judge 分数只是待核对输入，不得用于一致性或校准结论。"
        ),
        "summary": {
            "total": len(records),
            "pending": sum(1 for item in records if item["humanReviewStatus"] == "PENDING"),
            "completed": 0,
        },
        "items": rich,
    }
    return payload, rich


def render_markdown(payload: dict[str, Any], items: list[dict[str, Any]]) -> str:
    out: list[str] = [
        "# Phase 3.1 人工复核评分表",
        "",
        "生成日期：2026-10-28。",
        "",
        "本表对应 Phase 3 的 9 条待复核记录。**当前人工分数为 0 条完成、9 条待填写**；"
        "表中的自动 Judge 分数只是待核对的输入，不能当作已确认结论，也不得在人工填写前"
        "计算 Judge–human 一致性或宣称校准完成。",
        "",
        "## 填写说明",
        "",
        "- 分数：`0` 不满足、`1` 部分满足、`2` 满足；该维度不适用时填 `null`。",
        "- 每个维度必须写一句理由，引用回答中的具体句子或上下文事实。",
        "- 只根据回答全文和给定上下文评分，不根据用例标题、自动分数或模型自我评价倒推。",
        "- 截断回答（`TRUNCATED`）的 `task_completion` 最高记 1：内容可能正确，但任务未完成。",
        "- 完成后把 `humanReviewStatus` 改为 `COMPLETED`，再另行汇总一致性；本表不预填结果。",
        "",
        "## 评分维度与示例",
        "",
        "示例用于说明锚点，不是这 9 条样本的评分。",
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
                f"## {index}. {item['caseId']}-r{item['runIndex']}（{item['severity']}）",
                "",
                f"- artifact：`{item['artifact']}`",
                f"- 自动 case_status：**{item['caseStatus']}**（原记录：{item['recordedHardStatus']}）",
                f"- completion_status：**{item['completionStatus']}**（finish_reason={item['finishReason']}）",
                f"- 自动判读理由：{item['caseReason']}",
                f"- 人工复核原因：{item['reviewReason']}",
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
                "### 自动 Judge（未确认）",
                "",
                "| 维度 | 原始分 | 完整性调整后 | 不适用 | 理由 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for metric in METRIC_ORDER:
            score = item["automaticScores"][metric]
            out.append(
                "| {label} | {raw} | {adjusted} | {na} | {reason} |".format(
                    label=METRIC_LABELS[metric],
                    raw=score["rawJudgeScore"],
                    adjusted=score["adjustedScore"],
                    na="是" if not score["applicable"] else "否",
                    reason=str(score["reason"]).replace("|", "／"),
                )
            )
        out.extend(["", "### 人工评分（待填写）", "", "| 维度 | 0 | 1 | 2 | 分数 | 理由 |", "| --- | --- | --- | --- | --- | --- |"])
        for metric in METRIC_ORDER:
            rubric = item["rubric"].get(metric, {})
            out.append(
                "| {label} | {s0} | {s1} | {s2} |  |  |".format(
                    label=METRIC_LABELS[metric],
                    s0=str(rubric.get("score0", "")).replace("|", "／"),
                    s1=str(rubric.get("score1", "")).replace("|", "／"),
                    s2=str(rubric.get("score2", "")).replace("|", "／"),
                )
            )
        out.extend(
            [
                "",
                "- 人工结论：`agree` / `disagree` / `needs-third-review`（待填）",
                "- 总体理由：（待填）",
                "",
            ]
        )

    out.extend(
        [
            "## 统计口径",
            "",
            f"- 待复核记录：{payload['summary']['total']} 条。",
            f"- 已完成人工评分：{payload['summary']['completed']} 条。",
            f"- 待填写：{payload['summary']['pending']} 条。",
            "",
            "9 条只是初步样本，分布缺少变化；即使全部填写，也不足以用加权 Kappa "
            "证明 Judge 已校准。结论应表述为“有限样本上的一致性观察”，并保留不确定性。",
            "",
        ]
    )
    return "\n".join(out)


def main() -> None:
    payload, items = build()
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    OUT_MD.write_text(render_markdown(payload, items), encoding="utf-8")
    print(f"wrote {OUT_JSON.relative_to(REPO_ROOT)}")
    print(f"wrote {OUT_MD.relative_to(REPO_ROOT)}")
    print(json.dumps(payload["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
