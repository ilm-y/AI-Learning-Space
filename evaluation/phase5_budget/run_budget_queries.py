"""Phase 5：预算裁剪下的「检索状态 vs 注入状态」验证执行器。

目标问题（不预设一定发生）：
    检索成功，但片段因上下文 Token 预算没有进入最终 Prompt 时，
    模型是否仍能正确区分「检索到了」与「最终上下文里没有」，
    以及是否会出现引用未注入片段（citation out-of-bounds）。

本脚本只做测量，不改生产代码：
- 真实链路：真实 Embedding + 真实 Qdrant + 生产 `ContextBuilder` + 真实 LLM；
- 构造边界：固定 chunk + 合成历史/记忆制造预算压力，同样走生产 `ContextBuilder`；
- 原始回答、最终 system instruction、注入/丢弃 chunk、引用校验结果全部落盘。

运行方式（在 `agent/` 目录下，保证 `app` 可导入）：

    python ../evaluation/phase5_budget/run_budget_queries.py --dry-run
    python ../evaluation/phase5_budget/run_budget_queries.py

`--dry-run` 只构建上下文、不调用模型，用于先确认预算压力是否真的把知识片段
裁掉；真实模型调用的结果不能由 dry-run 推断。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from app.citations import format_source_label, provided_labels_from_context, validate_citations
from app.config import Settings
from app.context.builder import ContextBuilder
from app.embedding import build_embedding_provider
from app.llm import ChatMessage, build_llm_adapter
from app.llm.base import LLMError
from app.prompts import BASE_SYSTEM_INSTRUCTION
from app.retriever.base import KnowledgeQuery, KnowledgeResult, KnowledgeStatus
from app.retriever.knowledge import KnowledgeSearchService
from app.schemas import (
    HistoryMessage,
    KnowledgeChunk,
    MemoryItem,
    MemoryType,
    MessageRole,
)
from app.state.agent_state import AgentState
from app.trace import RunTrace
from app.vectorstore.qdrant_store import QdrantDocumentStore, collection_name_for

EVAL_ROOT = Path(__file__).resolve().parent
DEFAULT_QUERIES = EVAL_ROOT / "queries.jsonl"
REPORTS_DIR = EVAL_ROOT / "reports"

BUDGET_STATES = {"SUCCESS_PARTIAL", "SUCCESS_BUT_DROPPED"}

# 硬口径：只匹配「本轮没有检索到资料 / 没有可用资料」这一类**检索状态**陈述。
# 它必须带「资料/文档/材料/内容/依据/片段」宾语；「没有检索到直接答案」属于
# 回答可得性，不是检索状态，不能计入。
_NO_RETRIEVAL_CLAIM = re.compile(
    r"没有(?:检索|搜索|查找|找|查)(?:到|出)[^。！？\n]{0,12}(?:资料|文档|材料|内容|依据|片段)"
    r"|未(?:能)?(?:检索|搜索|查找|找|查)(?:到|出)[^。！？\n]{0,12}(?:资料|文档|材料|内容|依据|片段)"
    r"|没有(?:任何)?(?:可用|相关|可参考|可查阅|可引用)(?:的)?(?:资料|文档|材料|内容|依据)"
    r"|无法(?:查阅|访问|查看|看到|使用|引用)(?:任何)?(?:上传)?(?:的)?(?:资料|文档|材料)"
)
# 软口径：「资料中没有提到 X」。它是文档内容陈述，可能只是描述已注入片段，
# 单独计数、供人工复核，不计入硬指标 numerator。
_CONTENT_ABSENCE_CLAIM = re.compile(
    r"(?:资料|文档|材料)(?:中|里)(?:并)?(?:没有|未)(?:提到|包含|提供|给出|写明)"
    r"|(?:资料|文档|材料)(?:中|里)(?:并)?(?:不存在)"
)
# 句子一旦同时出现这些词，就视为在解释「预算/上下文没有放入」，不是误报检索为空。
_BUDGET_QUALIFIER = re.compile(
    r"预算|上下文|未(?:能)?放入|未(?:能)?包含|没有放入|没有包含|未进入|截断"
    r"|篇幅|token|Token|TOKEN|prompt|Prompt|丢弃|挤出|看不到"
)
_QUOTED_OR_HYPOTHETICAL = re.compile(r"如果|假设|若|你说|你问|问题里|题面|有人会")
# 「这不等于『没有检索到资料』」「我不能说『没有检索到资料』」是否定/引述，
# 不是模型在断言检索为空。
_NEGATED_CLAIM_PREFIX = re.compile(
    r"(?:不等于|并非|而不是|不是|不能[^\n]{0,6}说|不能写成|不能理解成|不要声称|不能把)"
    r"[^。！？\n]{0,8}[「『\"“]?\s*$"
)


def _load_queries(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _stem(name: str | None) -> str:
    if not name:
        return ""
    return name.rsplit("/", 1)[-1].rsplit(".", 1)[0].strip().lower()


def _long_history() -> list[HistoryMessage]:
    """12 条合成历史，每条约 900 字符；正文不含任何评测问题的事实答案。"""
    filler = (
        "这是本轮隔离测试中用于占据上下文预算的合成历史，不包含本轮问题的"
        "事实答案，也不包含任何文档结论。"
    )
    body = filler * 24
    messages: list[HistoryMessage] = []
    for index in range(12):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        messages.append(
            HistoryMessage(role=role, content=f"[合成历史 {index + 1}] {body}")
        )
    return messages


def _memories() -> list[MemoryItem]:
    """4 条合成长期记忆，用途同样是制造预算压力。"""
    text = (
        "这是用于占据上下文预算的合成长期记忆，不包含本轮问题的事实答案。"
    ) * 12
    return [
        MemoryItem(
            id=980 + index,
            memory_type=MemoryType.PREFERENCE,
            content=f"[合成记忆 {index + 1}] {text}"[:500],
        )
        for index in range(4)
    ]


def _fixed_chunks(spec: dict[str, Any]) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for item in spec.get("chunks", []):
        repeat = max(1, int(item.get("repeat", 1)))
        content = (str(item["content"]) + "\n") * repeat
        chunks.append(
            KnowledgeChunk(
                id=int(item["id"]),
                document_id=int(item["documentId"]),
                chunk_index=int(item["chunkIndex"]),
                document_name=str(item["documentName"]),
                content=content.strip(),
            )
        )
    return chunks


def _status_misstatement(answer: str) -> tuple[list[str], list[str], bool]:
    """返回 (硬口径命中句, 软口径命中句, 是否明确提到预算)。

    硬口径：「没有检索到资料」= 检索状态误述，进入指标 numerator。
    软口径：「资料中没有提到 X」= 文档内容陈述，单独报告、不计入硬指标。
    两者都是**关键词筛查**，不是语义判断；真实回答原样落盘，供人工复核。
    """
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?\n；;])", answer or "")]
    hard_hits: list[str] = []
    soft_hits: list[str] = []
    for sentence in sentences:
        if not sentence:
            continue
        if _BUDGET_QUALIFIER.search(sentence):
            continue
        if _QUOTED_OR_HYPOTHETICAL.search(sentence):
            continue
        hard_match = _NO_RETRIEVAL_CLAIM.search(sentence)
        soft_match = _CONTENT_ABSENCE_CLAIM.search(sentence)
        match = hard_match or soft_match
        if match is None:
            continue
        if _NEGATED_CLAIM_PREFIX.search(sentence[: match.start()]):
            continue
        if hard_match is not None:
            hard_hits.append(sentence)
        else:
            soft_hits.append(sentence)
    discloses_budget = bool(
        _BUDGET_QUALIFIER.search(answer or "")
        and re.search(r"检索|命中|找到|搜到", answer or "")
    )
    return hard_hits, soft_hits, discloses_budget


class RetrieverSetup:
    def __init__(self, settings: Settings, collection_prefix: str) -> None:
        self.settings = settings
        self.embedding_provider = build_embedding_provider(settings)
        self.qdrant_store = QdrantDocumentStore(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout_seconds=settings.qdrant_timeout_seconds,
            collection_prefix=collection_prefix,
            upsert_batch_size=settings.qdrant_upsert_batch_size,
        )
        self.search = KnowledgeSearchService(
            provider=self.embedding_provider,
            store=self.qdrant_store,
            collection_prefix=collection_prefix,
            timeout_seconds=settings.knowledge_timeout_seconds,
        )
        self.collection = collection_name_for(
            collection_prefix,
            self.embedding_provider.model,
            self.embedding_provider.dimension,
        )
        self.client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=settings.qdrant_timeout_seconds,
        )

    async def discover_documents(self, user_id: int, space_id: int) -> dict[str, dict[str, Any]]:
        """返回 document_id -> {name, chunks}，只读；用于确认真实语料可见。"""
        flt = qm.Filter(
            must=[
                qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
                qm.FieldCondition(key="space_id", match=qm.MatchValue(value=space_id)),
            ]
        )
        found: dict[str, dict[str, Any]] = {}
        offset = None
        while True:
            points, offset = await self.client.scroll(
                collection_name=self.collection,
                scroll_filter=flt,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                document_id = payload.get("document_id")
                name = payload.get("file_name") or payload.get("document_name")
                if not isinstance(document_id, int) or not isinstance(name, str):
                    continue
                entry = found.setdefault(str(document_id), {"name": name, "chunks": 0})
                entry["chunks"] += 1
            if offset is None:
                break
        return found

    async def aclose(self) -> None:
        await self.client.close()
        await self.qdrant_store.aclose()
        await self.embedding_provider.aclose()


def _build_state(
    *,
    run_id: int,
    user_id: int,
    space_id: int,
    question: str,
    chunks: list[KnowledgeChunk],
    status: str,
    warning: str | None,
    scenario: dict[str, Any],
) -> AgentState:
    return AgentState(
        run_id=run_id,
        user_id=user_id,
        conversation_id=run_id,
        space_id=space_id,
        user_message=question,
        history=_long_history()[: int(scenario.get("history", 0))],
        memories=_memories()[: int(scenario.get("memories", 0))],
        knowledge=chunks,
        knowledge_status=status,
        knowledge_warning=warning,
        trace=RunTrace(run_id=run_id),
    )


def _expected_docs(
    expected: list[str], retrieved: list[KnowledgeChunk], injected_ids: list[int]
) -> tuple[bool, bool]:
    wanted = {_stem(name) for name in expected if name}
    retrieved_stems = {_stem(chunk.document_name) for chunk in retrieved}
    injected_stems = {
        _stem(chunk.document_name) for chunk in retrieved if chunk.id in set(injected_ids)
    }
    return wanted.issubset(retrieved_stems), wanted.issubset(injected_stems)


def _citation_judgment(
    answer: str, chunks: list[KnowledgeChunk], labels: list[str], dropped_ids: list[int]
) -> dict[str, Any]:
    report = validate_citations(answer, chunks, provided_labels=labels)
    dropped_labels = {
        format_source_label(chunk) for chunk in chunks if chunk.id in set(dropped_ids)
    }
    dropped_citations = [
        citation.raw
        for citation in report.citations
        if not citation.valid and citation.raw in dropped_labels
    ]
    return {
        "citations": report.as_dict(),
        "citationOob": report.invalid > 0,
        "citationInvalidCount": report.invalid,
        "citationDroppedCount": len(dropped_citations),
        "citationDroppedLabels": dropped_citations,
    }


def _judge_run(
    *,
    scenario_name: str,
    context: Any,
    answer: str,
    finish_reason: str | None,
    error: str | None,
    retrieval_status: str,
    chunks: list[KnowledgeChunk],
    expected_docs: tuple[bool, bool],
    citation: dict[str, Any] | None,
) -> dict[str, Any]:
    knowledge_state = context.knowledge_state
    budget_trimmed = knowledge_state in BUDGET_STATES and retrieval_status == "SUCCESS"
    budget_scenario = scenario_name != "normal"
    empty_answer = not (answer or "").strip()
    truncated = finish_reason == "length"
    expected_retrieved, expected_injected = expected_docs

    status_sentences: list[str] = []
    absence_sentences: list[str] = []
    discloses_budget = False
    if answer and not error:
        status_sentences, absence_sentences, discloses_budget = _status_misstatement(answer)
    status_hit = bool(status_sentences)

    citation_oob = bool(citation and citation["citationOob"])
    target_error = status_hit or citation_oob

    eligible = False
    ineligible_reason = None
    if error:
        ineligible_reason = f"model_error: {error}"
    elif empty_answer:
        ineligible_reason = "empty_answer"
    elif truncated:
        ineligible_reason = "finish_reason_length"
    elif not budget_scenario:
        ineligible_reason = "not_a_budget_scenario"
    elif retrieval_status != "SUCCESS":
        ineligible_reason = f"retrieval_not_success: {retrieval_status}"
    elif not budget_trimmed:
        ineligible_reason = "pressure_inadequate"
    else:
        eligible = True

    return {
        "knowledgeState": knowledge_state,
        "retrievalStatus": retrieval_status,
        "budgetTrimmed": budget_trimmed,
        "expectedDocsRetrieved": expected_retrieved,
        "expectedDocsInjected": expected_injected,
        "answerChars": len(answer or ""),
        "finishReason": finish_reason,
        "truncated": truncated,
        "statusMisstatement": status_hit,
        "statusMisstatementSentences": status_sentences,
        "contentAbsenceClaim": bool(absence_sentences),
        "contentAbsenceClaimSentences": absence_sentences,
        "disclosesBudget": discloses_budget,
        "citationOob": citation_oob,
        "citationInvalidCount": int(citation["citationInvalidCount"]) if citation else 0,
        "citationDroppedCount": int(citation["citationDroppedCount"]) if citation else 0,
        "citationDroppedLabels": list(citation["citationDroppedLabels"]) if citation else [],
        "targetError": target_error,
        "eligibleForMetric": eligible,
        "ineligibleReason": ineligible_reason,
        "semanticReview": "PENDING",
    }


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [run for run in runs if run["judgment"].get("eligibleForMetric")]
    errors = [run for run in eligible if run["judgment"].get("targetError")]
    status_errors = [run for run in eligible if run["judgment"].get("statusMisstatement")]
    citation_errors = [run for run in eligible if run["judgment"].get("citationOob")]
    absence_claims = [
        run for run in eligible if run["judgment"].get("contentAbsenceClaim")
    ]
    budget_disclosures = [
        run for run in eligible if run["judgment"].get("disclosesBudget")
    ]
    expected_retrieved = [
        run for run in eligible if run["judgment"].get("expectedDocsRetrieved")
    ]
    expected_injected = [
        run for run in eligible if run["judgment"].get("expectedDocsInjected")
    ]

    def rate(part: list[dict[str, Any]]) -> float | None:
        return round(len(part) / len(eligible), 4) if eligible else None

    return {
        "runsTotal": len(runs),
        "eligibleBudgetRuns": len(eligible),
        "targetErrorRuns": len(errors),
        "targetErrorRate": rate(errors),
        "statusMisstatementRuns": len(status_errors),
        "statusMisstatementRate": rate(status_errors),
        "citationOobRuns": len(citation_errors),
        "citationOobRate": rate(citation_errors),
        "contentAbsenceClaimRuns": len(absence_claims),
        "contentAbsenceClaimRate": rate(absence_claims),
        "disclosesBudgetRuns": len(budget_disclosures),
        "disclosesBudgetRate": rate(budget_disclosures),
        "expectedDocsRetrievedRuns": len(expected_retrieved),
        "expectedDocsRetrievedRate": rate(expected_retrieved),
        "expectedDocsInjectedRuns": len(expected_injected),
        "expectedDocsInjectedRate": rate(expected_injected),
        "ineligible": [
            {
                "runKey": run["runKey"],
                "reason": run["judgment"].get("ineligibleReason"),
            }
            for run in runs
            if not run["judgment"].get("eligibleForMetric")
        ],
        "errors": [
            {
                "runKey": run["runKey"],
                "question": run["question"],
                "statusSentences": run["judgment"].get("statusMisstatementSentences"),
                "citationDroppedLabels": run["judgment"].get("citationDroppedLabels", []),
                "answerExcerpt": run["answer"][:400],
            }
            for run in errors
        ],
        "softAbsenceClaims": [
            {
                "runKey": run["runKey"],
                "question": run["question"],
                "sentences": run["judgment"].get("contentAbsenceClaimSentences"),
                "answerExcerpt": run["answer"][:400],
            }
            for run in absence_claims
        ],
    }


def _write_markdown(path: Path, summary: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    agg = summary["aggregate"]
    lines = [
        "# Phase 5 预算裁剪验证结果",
        "",
        f"- 生成时间：{summary['generatedAt']}",
        f"- 模型：`{summary['config']['model']}`，temperature={summary['config']['temperature']}，"
        f"max_tokens={summary['config']['maxTokens']}",
        f"- 代码版本：`{summary['config']['gitHead']}`",
        f"- 语料：{summary['config']['collection']}",
        f"- 总运行：{agg['runsTotal']}；计入指标：{agg['eligibleBudgetRuns']}",
        "",
        "## 指标",
        "",
        "| 指标 | 分子/分母 | 结果 |",
        "| --- | --- | --- |",
        f"| 目标错误行为率 | {agg['targetErrorRuns']}/{agg['eligibleBudgetRuns']} | {agg['targetErrorRate']} |",
        f"| 证据状态误述率 | {agg['statusMisstatementRuns']}/{agg['eligibleBudgetRuns']} | {agg['statusMisstatementRate']} |",
        f"| 引用越界率 | {agg['citationOobRuns']}/{agg['eligibleBudgetRuns']} | {agg['citationOobRate']} |",
        f"| 软口径「资料中没有」陈述（不计入指标） | {agg['contentAbsenceClaimRuns']}/{agg['eligibleBudgetRuns']} | {agg['contentAbsenceClaimRate']} |",
        f"| 明确说明预算/上下文未包含 | {agg['disclosesBudgetRuns']}/{agg['eligibleBudgetRuns']} | {agg['disclosesBudgetRate']} |",
        f"| 期望文档确实被检索到 | {agg['expectedDocsRetrievedRuns']}/{agg['eligibleBudgetRuns']} | {agg['expectedDocsRetrievedRate']} |",
        f"| 期望文档进入最终上下文 | {agg['expectedDocsInjectedRuns']}/{agg['eligibleBudgetRuns']} | {agg['expectedDocsInjectedRate']} |",
        "",
        "> 指标只统计「明确标注的预算裁剪场景」且回答非空、未被截断的运行；",
        "> 未达到预算压力的运行列入 `ineligible`，不进入分母。",
        "> 状态误述是关键词筛查，语义结论仍需人工复核；本轮 `semanticReview` 全部为 `PENDING`。",
        "",
        "## 运行明细",
        "",
        "| runKey | 场景 | 检索状态 | 注入状态 | 注入/丢弃 | 错误 | 回答字数 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        j = run["judgment"]
        notes = []
        if j.get("statusMisstatement"):
            notes.append("状态误述?")
        if j.get("citationOob"):
            notes.append("引用越界")
        if j.get("ineligibleReason"):
            notes.append(j["ineligibleReason"])
        lines.append(
            f"| {run['runKey']} | {run['scenario']['name']} | {j.get('retrievalStatus')} | "
            f"{j.get('knowledgeState')} | {len(run['context']['injectedChunkIds'])}/"
            f"{len(run['context']['droppedChunkIds'])} | {j.get('targetError')} | "
            f"{j.get('answerChars')} | {'；'.join(notes)} |"
        )
    lines.extend(
        [
            "",
            "## 不可计入的运行",
            "",
            "```json",
            json.dumps(agg["ineligible"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## 疑似目标错误",
            "",
            "```json",
            json.dumps(agg["errors"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## 软口径「资料中没有」陈述（不计入指标）",
            "",
            "```json",
            json.dumps(agg["softAbsenceClaims"], ensure_ascii=False, indent=2),
            "```",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def recompute_from(directory: Path, out_dir: Path | None = None) -> None:
    """只对已保存的原始回答重跑确定性筛查，不重新调用模型。

    用途：筛查正则修订后，用同一批原始回答得到 v2 判定；模型输出本身不变。
    """
    runs_path = directory / "runs.jsonl"
    summary_path = directory / "summary.json"
    if not runs_path.exists() or not summary_path.exists():
        raise SystemExit(f"目录缺少 runs.jsonl / summary.json：{directory}")

    runs = [
        json.loads(line)
        for line in runs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for run in runs:
        judgment = run["judgment"]
        finish_reason = (run.get("diagnostics") or {}).get("finishReason") or judgment.get(
            "finishReason"
        )
        run["judgment"] = _judge_run(
            scenario_name=run["scenario"]["name"],
            context=SimpleNamespace(knowledge_state=run["context"]["knowledgeState"]),
            answer=run.get("answer", ""),
            finish_reason=finish_reason,
            error=run.get("llmError"),
            retrieval_status=run["retrieval"]["status"],
            chunks=[],
            expected_docs=(
                bool(judgment.get("expectedDocsRetrieved")),
                bool(judgment.get("expectedDocsInjected")),
            ),
            citation=run.get("citation"),
        )
        run["judgment"]["screeningVersion"] = 2

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["aggregate"] = _aggregate(runs)
    summary["screeningVersion"] = 2
    summary["screeningNote"] = (
        "v2 硬口径只统计「没有检索到资料」类检索状态陈述；"
        "「资料中没有提到 X」作为软口径单独报告，不计入指标。"
    )

    target = out_dir or directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "runs.v2.jsonl").write_text(
        "\n".join(json.dumps(run, ensure_ascii=False) for run in runs) + "\n",
        encoding="utf-8",
    )
    (target / "summary.v2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_markdown(target / "summary.v2.md", summary, runs)
    print(json.dumps({"out": str(target), "runs": len(runs)}, ensure_ascii=False))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--user-id", type=int, default=14)
    parser.add_argument("--space-id", type=int, default=22)
    parser.add_argument("--collection-prefix", default="ails_phase42_real_e2e")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--max-context", type=int, default=None)
    parser.add_argument("--ids", default=None, help="逗号分隔的用例 id，只跑这些")
    parser.add_argument("--scenarios", default=None, help="逗号分隔的场景名，只跑这些")
    parser.add_argument("--dry-run", action="store_true", help="只构建上下文，不调用模型")
    parser.add_argument(
        "--recompute-from",
        type=Path,
        default=None,
        help="不调用模型，只对目录里的 runs.jsonl 重跑 v2 筛查",
    )
    args = parser.parse_args()

    if args.recompute_from is not None:
        recompute_from(args.recompute_from, args.out)
        return

    settings = Settings()
    if not args.dry_run:
        if settings.llm_provider != "openai" or not settings.llm_api_key:
            raise SystemExit("真实 LLM 未配置：需要 AILS_LLM_PROVIDER=openai 且提供 API Key")

    queries = _load_queries(args.queries)
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        queries = [item for item in queries if item["id"] in wanted]
    wanted_scenarios = (
        {item.strip() for item in args.scenarios.split(",") if item.strip()}
        if args.scenarios
        else None
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out or (REPORTS_DIR / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    setup = RetrieverSetup(settings, args.collection_prefix)
    adapter = None
    try:
        documents = await setup.discover_documents(args.user_id, args.space_id)
        if not documents:
            raise SystemExit(
                f"Qdrant 中没有 user={args.user_id} space={args.space_id} 的文档；"
                f"collection={setup.collection}"
            )
        allowed_ids = sorted(int(key) for key in documents)
        corpus = {
            str(document_id): info for document_id, info in documents.items()
        }

        builder = ContextBuilder(
            max_context_tokens=args.max_context or settings.max_context_tokens,
            recent_history_limit=settings.recent_history_limit,
        )
        adapter = None if args.dry_run else build_llm_adapter(settings)

        runs: list[dict[str, Any]] = []
        run_seq = 5000
        for case in queries:
            for scenario in case.get("scenarios", []):
                if wanted_scenarios is not None and scenario.get("name") not in wanted_scenarios:
                    continue
                run_seq += 1
                run_key = f"{case['id']}::{scenario['name']}"

                if case["retrieval"]["mode"] == "fixed":
                    chunks = _fixed_chunks(case["retrieval"])
                    retrieval_status = KnowledgeStatus.SUCCESS.value
                    retrieval_warning = None
                    retrieval_candidates = len(chunks)
                    retrieval_error = None
                else:
                    result: KnowledgeResult = await setup.search.search(
                        KnowledgeQuery(
                            user_id=args.user_id,
                            space_id=args.space_id,
                            question=case["question"],
                            top_k=settings.knowledge_top_k,
                            min_score=settings.knowledge_min_score,
                        ),
                        allowed_ids,
                    )
                    chunks = list(result.chunks)
                    retrieval_status = result.status.value
                    retrieval_warning = result.warning
                    retrieval_candidates = result.candidates
                    retrieval_error = None

                state = _build_state(
                    run_id=run_seq,
                    user_id=args.user_id,
                    space_id=args.space_id,
                    question=case["question"],
                    chunks=chunks,
                    status=retrieval_status,
                    warning=retrieval_warning,
                    scenario=scenario,
                )
                context = builder.build(state)
                labels = provided_labels_from_context(context, chunks)
                expected_docs = _expected_docs(
                    case.get("expectedRelevantDocuments", []),
                    chunks,
                    context.knowledge_injected_chunk_ids,
                )

                answer = ""
                finish_reason = None
                llm_error = None
                diagnostics: dict[str, Any] | None = None
                citation: dict[str, Any] | None = None

                if not args.dry_run:
                    try:
                        answer = await adapter.complete(
                            [
                                ChatMessage(role=message.role, content=message.content)
                                for message in context.messages
                            ],
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                        )
                        raw_diagnostics = getattr(adapter, "last_completion_diagnostics", None)
                        if raw_diagnostics is not None:
                            diagnostics = raw_diagnostics.as_dict()
                            finish_reason = raw_diagnostics.finish_reason
                        citation = _citation_judgment(
                            answer,
                            chunks,
                            labels,
                            context.knowledge_dropped_chunk_ids,
                        )
                    except LLMError as exc:
                        llm_error = f"{type(exc).__name__}: {exc}"
                    except Exception as exc:  # noqa: BLE001 —— 记录后继续，不掩盖样本失败
                        llm_error = f"{type(exc).__name__}: {exc}"

                judgment = _judge_run(
                    scenario_name=scenario["name"],
                    context=context,
                    answer=answer,
                    finish_reason=finish_reason,
                    error=llm_error,
                    retrieval_status=retrieval_status,
                    chunks=chunks,
                    expected_docs=expected_docs,
                    citation=citation,
                )
                if args.dry_run:
                    judgment.update(
                        {
                            "targetError": None,
                            "eligibleForMetric": False,
                            "ineligibleReason": "dry_run",
                            "semanticReview": "NOT_RUN",
                        }
                    )

                run = {
                    "runKey": run_key,
                    "caseId": case["id"],
                    "origin": case["origin"],
                    "realUserTraffic": bool(case.get("realUserTraffic", False)),
                    "kind": case.get("kind"),
                    "question": case["question"],
                    "scenario": scenario,
                    "retrieval": {
                        "mode": case["retrieval"]["mode"],
                        "status": retrieval_status,
                        "warning": retrieval_warning,
                        "candidates": retrieval_candidates,
                        "chunks": [
                            {
                                "chunkId": chunk.id,
                                "documentId": chunk.document_id,
                                "chunkIndex": chunk.chunk_index,
                                "documentName": chunk.document_name,
                                "score": chunk.score,
                                "contentChars": len(chunk.content),
                                "content": chunk.content[:2000],
                            }
                            for chunk in chunks
                        ],
                    },
                    "context": {
                        "knowledgeStatus": context.knowledge_status,
                        "knowledgeState": context.knowledge_state,
                        "injectedChunkIds": context.knowledge_injected_chunk_ids,
                        "droppedChunkIds": context.knowledge_dropped_chunk_ids,
                        "contextTokens": context.context_tokens,
                        "budgetTokens": context.budget_tokens,
                        "estimatedTokens": context.estimated_tokens,
                        "budgetExceeded": context.budget_exceeded,
                        "providedLabels": labels,
                        "warnings": context.warnings,
                        "sections": [section.as_dict() for section in context.sections],
                        "systemInstruction": context.system_instruction,
                        "messages": [message.as_dict() for message in context.messages],
                    },
                    "answer": answer,
                    "llmError": llm_error,
                    "diagnostics": diagnostics,
                    "citation": citation,
                    "judgment": judgment,
                }
                runs.append(run)
                print(
                    json.dumps(
                        {
                            "run": run_key,
                            "retrieval": retrieval_status,
                            "state": context.knowledge_state,
                            "injected": len(context.knowledge_injected_chunk_ids),
                            "dropped": len(context.knowledge_dropped_chunk_ids),
                            "chars": len(answer),
                            "targetError": judgment.get("targetError"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        git_head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=EVAL_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip() or "unknown"

        summary = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "phase": "phase5_budget",
            "dryRun": args.dry_run,
            "config": {
                "gitHead": git_head,
                "model": settings.llm_model,
                "provider": settings.llm_provider,
                "temperature": args.temperature,
                "maxTokens": args.max_tokens,
                "maxContextTokens": args.max_context or settings.max_context_tokens,
                "embeddingModel": settings.embedding_model,
                "embeddingDimension": settings.embedding_dimension,
                "collection": setup.collection,
                "collectionPrefix": args.collection_prefix,
                "userIds": [args.user_id],
                "spaceIds": [args.space_id],
                "allowedDocumentIds": allowed_ids,
                "promptSha256": hashlib.sha256(
                    BASE_SYSTEM_INSTRUCTION.encode("utf-8")
                ).hexdigest()[:16],
                "corpus": corpus,
                "queryFile": str(args.queries),
            },
            "aggregate": _aggregate(runs),
        }

        (out_dir / "runs.jsonl").write_text(
            "\n".join(json.dumps(run, ensure_ascii=False) for run in runs) + "\n",
            encoding="utf-8",
        )
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_markdown(out_dir / "summary.md", summary, runs)
        print(json.dumps({"out": str(out_dir), "runs": len(runs)}, ensure_ascii=False))
    finally:
        if adapter is not None:
            await adapter.aclose()
        await setup.aclose()


if __name__ == "__main__":
    asyncio.run(main())
