"""生成 Phase 4.1 盲评表与机器可读队列。

只读实际运行产物：
- Phase 4 `agent_results.json`（5 个真实模型用例）
- Phase 4.1 `budget_pressure_llm.json`（1 个受控预算压力的真实模型用例）

不读取、不写入任何 Judge 分数；`humanScores` 全部为 null。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHASE4 = ROOT / "evaluation/phase4/reports/20261027_172403/agent_results.json"
BUDGET = ROOT / "evaluation/phase4_1/budget_pressure_llm.json"
SOURCE_LLM = ROOT / "evaluation/phase4_1/source_attribution_llm.json"
MD_OUT = ROOT / "docs/testing/phase4.1-agent-human-review.md"
JSON_OUT = ROOT / "evaluation/phase4_1/human-review-queue.json"

DOC_BY_CHUNK = {18: 20, 19: 21}

CHUNK_FACTS = {
    18: (
        "document_id=20，chunk_id=18，`orbit_current_20261027_172403.md`：现行规范；"
        "当前调度器使用 17 秒重试窗口，从 2025 版起生效、适用于所有新部署；"
        "校验种子 0x7F3A；不要使用历史迁移文档中的旧值；若 17 秒不符合预期，"
        "应先核对部署版本，而不是改回旧值。"
    ),
    19: (
        "document_id=21，chunk_id=19，`orbit_legacy_20261027_172403.md`："
        "2024 年及以前的旧调度器使用 12 秒重试窗口，该版本已在 2025 年下线；"
        "只用于迁移考古，不适用于当前部署；不要把 12 秒当作现行值。"
    ),
}

RUBRIC = {
    "factual_correctness": (
        "0 = 核心事实错误或编造；1 = 部分正确但有遗漏/过度概括；"
        "2 = 在给定上下文范围内事实正确"
    ),
    "source_attribution": (
        "0 = 把事实归给不支持的来源，或给出不存在的引用；1 = 有拆分但仍有"
        "一句混合多来源；2 = 每个事实都归到支持它的来源，或明确说明来源未覆盖"
    ),
    "knowledge_state_honesty": (
        "0 = 把「检索成功但预算未注入」说成「没有检索到」，或把资料没写的内容"
        "说成资料结论；1 = 表述含糊；2 = 准确区分检索成功/无匹配/预算未注入"
    ),
    "task_completion": (
        "0 = 没有回答问题；1 = 只回答一部分或没有说明缺口；"
        "2 = 回答所有可回答部分，并明确说明无法回答的部分"
    ),
}


def _load() -> tuple[dict[str, dict], dict, dict]:
    phase4 = {item["key"]: item for item in json.loads(PHASE4.read_text(encoding="utf-8"))}
    budget = json.loads(BUDGET.read_text(encoding="utf-8"))
    source_llm = json.loads(SOURCE_LLM.read_text(encoding="utf-8"))
    return phase4, budget, source_llm


def _citation_rows(item: dict | None) -> list[dict]:
    if not item:
        return []
    rows = []
    for citation in item["citations"]["citations"]:
        rows.append(
            {
                "label": citation["raw"],
                "chunkId": citation["chunkId"],
                "documentId": DOC_BY_CHUNK.get(citation["chunkId"]),
                "claim": citation["claim"],
                "sourceExcerpt": citation["sourceExcerpt"],
            }
        )
    return rows


def _source_text(chunk_id: int, content: str, injected: bool) -> dict:
    return {
        "chunkId": chunk_id,
        "documentId": DOC_BY_CHUNK[chunk_id],
        "injected": injected,
        "content": content,
    }


def _item(
    item_id: str,
    source: str,
    question: str,
    answer: str,
    context_facts: list[str],
    citations: list[dict],
    source_texts: list[dict] | None = None,
    note: str = "",
) -> dict:
    return {
        "id": item_id,
        "source": source,
        "question": question,
        "answer": answer,
        "contextFacts": context_facts,
        "sourceTexts": source_texts or [],
        "citations": citations,
        "note": note,
        "humanScores": {
            "factualCorrectness": None,
            "sourceAttribution": None,
            "knowledgeStateHonesty": None,
            "taskCompletion": None,
        },
        "humanDecision": None,
        "humanNotes": None,
        "humanReviewStatus": "PENDING",
    }


def build_items() -> list[dict]:
    phase4, budget, source_llm = _load()
    texts = {chunk["chunkId"]: chunk["content"] for chunk in budget["knowledgeChunks"]}
    injected = [
        _source_text(18, texts[18], injected=True),
        _source_text(19, texts[19], injected=True),
    ]
    dropped = [
        _source_text(18, texts[18], injected=False),
        _source_text(19, texts[19], injected=False),
    ]
    items: list[dict] = []

    supported = phase4["supported"]
    items.append(
        _item(
            "P41-01-current-knowledge",
            "Phase 4 `supported`，真实检索 + 真实 LLM",
            supported["question"],
            supported["answer"],
            [
                CHUNK_FACTS[18],
                CHUNK_FACTS[19],
                "最终注入 chunk：18、19；system instruction 含两个来源标签。",
            ],
            _citation_rows(supported),
            source_texts=injected,
        )
    )

    conflicting = phase4["conflicting_versions"]
    items.append(
        _item(
            "P41-02-conflicting-versions",
            "Phase 4 `conflicting_versions`，真实检索 + 真实 LLM",
            conflicting["question"],
            conflicting["answer"],
            [
                CHUNK_FACTS[18],
                CHUNK_FACTS[19],
                "最终注入 chunk：18、19。",
            ],
            _citation_rows(conflicting),
            source_texts=injected,
        )
    )

    items.append(
        _item(
            "P41-03-budget-insufficient",
            "Phase 4.1 受控上下文（固定 chunk，真实 LLM）",
            budget["question"],
            budget["answer"],
            [
                CHUNK_FACTS[18],
                CHUNK_FACTS[19],
                "检索状态 SUCCESS；最终注入 0 个知识 chunk；18、19 都因 Token "
                "预算被丢弃；system instruction 含 SUCCESS_BUT_DROPPED 说明。"
                f"动态上下文约 {budget['context']['contextTokens']} / "
                f"{budget['context']['budgetTokens']} tokens。",
            ],
            [],
            source_texts=dropped,
            note=(
                "这是一条受控预算压力样本：知识片段是 Phase 4 的固定合成 chunk，"
                "没有重新调用 Qdrant/Embedding；长历史与长期记忆是合成压力数据；"
                "模型调用是真实的。不得把它写成在线 RAG 召回结果。"
            ),
        )
    )

    no_match = phase4["no_match"]
    items.append(
        _item(
            "P41-04-no-match",
            "Phase 4 `no_match`，真实检索 + 真实 LLM",
            no_match["question"],
            no_match["answer"],
            [
                "检索状态 THRESHOLD_FILTERED；候选均低于阈值；最终注入 0 个知识 chunk。",
                "没有可引用的上传资料。",
            ],
            [],
        )
    )

    legacy = phase4["legacy_only"]
    items.append(
        _item(
            "P41-05-source-attribution",
            "Phase 4 `legacy_only`，真实检索 + 真实 LLM",
            legacy["question"],
            legacy["answer"],
            [
                CHUNK_FACTS[18],
                CHUNK_FACTS[19],
                "最终注入 chunk：18、19。",
            ],
            _citation_rows(legacy),
            source_texts=injected,
        )
    )

    insufficient = phase4["insufficient_detail"]
    items.append(
        _item(
            "P41-06-insufficient-detail",
            "Phase 4 `insufficient_detail`，真实检索 + 真实 LLM",
            insufficient["question"],
            insufficient["answer"],
            [
                CHUNK_FACTS[18],
                CHUNK_FACTS[19],
                "两份资料都没有提到仪表盘默认颜色；最终注入 chunk：18、19。",
            ],
            _citation_rows(insufficient),
            source_texts=injected,
        )
    )

    # 修复后的真实模型样本：每种形态取 3 次调用中的第 1 次；不展示自动诊断。
    for suffix, case_key, context_fact in (
        (
            "07-postfix-insufficient-detail",
            "insufficient_detail",
            "两份资料都没有提到仪表盘默认颜色；最终注入 chunk：18、19。",
        ),
        (
            "08-postfix-legacy-only",
            "legacy_only",
            "问题问的是 2024 年及以前旧调度器的值；最终注入 chunk：18、19。",
        ),
    ):
        case = next(
            row
            for row in source_llm["cases"]
            if row["key"] == case_key and row["trial"] == 1
        )
        items.append(
            _item(
                f"P41-{suffix}",
                "Phase 4.1 修复后 Prompt，真实模型样本（受控上下文，3 次中的第 1 次）",
                case["question"],
                case["answer"],
                [CHUNK_FACTS[18], CHUNK_FACTS[19], context_fact],
                _citation_rows(case),
                source_texts=injected,
                note=(
                    "知识片段固定复用 Phase 4 的合成 chunk，没有重新调用 "
                    "Qdrant/Embedding；Prompt 已加入「不同来源/版本拆句并分别标注」"
                    "规则。模型调用是真实的，但样本有随机性，不能把单次输出当成"
                    "稳定行为。"
                ),
            )
        )
    return items


def render_markdown(items: list[dict]) -> str:
    lines: list[str] = [
        "# Phase 4.1 真实回答盲评表（RAG 来源归属与知识状态）",
        "",
        "生成日期：2026-10-28。",
        "",
        "本表新增 8 条 Phase 4/4.1 真实模型回答，用于人工判断：来源归属、"
        "知识状态说明、资料不足时的边界。**当前新增 8 条人工评分为 0/8。**",
        "",
        "Phase 3.2/3.3 的 9 条盲评队列保持不变，仍为 **0/9**；两批合计"
        " **0/17**。本表不显示自动 Judge 分数、自动 `case_status`、"
        "`numericConflict` 等自动诊断，避免锚定人工判断。",
        "",
        "建议流程：先只用本表独立评分；全部填写后再与自动校验结果对照，"
        "并单独记录一致/不一致。**在人工分数填写前，不计算 Judge–human 一致率、"
        "Kappa 或「Judge 已校准」。**",
        "",
        "## 填写说明",
        "",
        "- 分数：`0` 不满足、`1` 部分满足、`2` 满足；该维度不适用时填 `null`。",
        "- 每个维度必须写一句理由，引用回答中的具体句子或给定上下文事实。",
        "- 只根据回答全文和给定上下文评分；不要根据用例编号、来源说明或"
        "自动校验结果倒推。",
        "- 本表不展示自动 `numericConflict` 信号；来源是否真的支持事实，由评分者判断。",
        "",
        "## 评分维度",
        "",
    ]
    for key, text in RUBRIC.items():
        lines.append(f"- `{key}`：{text}")
    lines.append("")

    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"## {index}. {item['id']}",
                "",
                f"- 来源：{item['source']}",
                f"- 用户输入：{item['question']}",
            ]
        )
        if item["note"]:
            lines.append(f"- 样本边界：{item['note']}")
        lines.extend(["", "### 必要上下文事实", ""])
        for fact in item["contextFacts"]:
            lines.append(f"- {fact}")
        if item["sourceTexts"]:
            lines.extend(["", "### 资料原文", ""])
            for source_text in item["sourceTexts"]:
                state = "本轮已注入" if source_text["injected"] else "本轮因预算未注入（模型未看到）"
                lines.extend(
                    [
                        f"**chunk_id={source_text['chunkId']} / "
                        f"document_id={source_text['documentId']}（{state}）**",
                        "",
                        "````text",
                        source_text["content"],
                        "````",
                        "",
                    ]
                )
        lines.extend(
            [
                "",
                "### 最终回答（原样，未修改）",
                "",
                "````text",
                item["answer"],
                "````",
                "",
                "### 回答中的来源标注",
                "",
            ]
        )
        if item["citations"]:
            lines.extend(
                [
                    "| 标注 | chunk_id | document_id | 被标注的句子 |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for citation in item["citations"]:
                claim = citation["claim"].replace("|", "\\|")
                lines.append(
                    f"| `{citation['label']}` | {citation['chunkId']} | "
                    f"{citation['documentId']} | {claim} |"
                )
        else:
            lines.append("本轮回答没有任何来源标注。")
        lines.extend(
            [
                "",
                "### 人工评分（PENDING）",
                "",
                "| 维度 | 0 | 1 | 2 | 分数 | 理由 |",
                "| --- | --- | --- | --- | --- | --- |",
                "| 事实正确性 | 核心事实错误或编造。 | 部分正确但有遗漏/过度概括。 | 在给定上下文范围内事实正确。 |  |  |",
                "| 来源归属 | 归给不支持的来源或引用不存在。 | 有拆分但仍有句子混合多来源。 | 每个事实归到支持它的来源，或明确来源未覆盖。 |  |  |",
                "| 知识状态诚实 | 把预算未注入说成没有检索到，或把资料没写的内容说成资料结论。 | 表述含糊。 | 准确区分检索成功/无匹配/预算未注入。 |  |  |",
                "| 任务完成度 | 没有回答问题。 | 只回答一部分或没有说明缺口。 | 回答所有可回答部分，并说明无法回答的部分。 |  |  |",
                "",
                "- 人工判定：`PASS` / `FAIL` / `NEEDS-THIRD-REVIEW`（待填）",
                "- 总体理由：（待填）",
                "- 评分人 / 日期：（待填）",
                "",
            ]
        )

    lines.extend(
        [
            "## 统计口径",
            "",
            "| 指标 | 数量 |",
            "| --- | --- |",
            "| Phase 4.1 新增待复核 | 8 |",
            "| Phase 4.1 已完成人工评分 | **0** |",
            "| Phase 3.2/3.3 待复核 | 9 |",
            "| Phase 3.2/3.3 已完成人工评分 | **0** |",
            "| 两批合计待复核 | 17 |",
            "| 两批合计已完成 | **0** |",
            "| 已计算 Judge–human 一致率 / Kappa | **否** |",
            "",
            "8 + 9 条只是小样本；即使全部填写，也不足以用加权 Kappa 证明 Judge 已校准。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    items = build_items()
    MD_OUT.write_text(render_markdown(items), encoding="utf-8")
    JSON_OUT.write_text(
        json.dumps(
            {
                "generatedAt": "2026-10-28",
                "phase": "4.1",
                "items": items,
                "carriedOver": {
                    "phase": "3.2/3.3",
                    "path": "evaluation/phase3/reports/human-review-queue-20261028.json",
                    "pending": 9,
                    "scored": 0,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {MD_OUT}")
    print(f"wrote {JSON_OUT}")
    print(f"items={len(items)} pending={sum(i['humanReviewStatus'] == 'PENDING' for i in items)}")


if __name__ == "__main__":
    main()
