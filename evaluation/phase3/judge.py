"""Phase 3 语义评分器。

这不是把关键词规则换一个名字。评分器把**用例自己的 0/1/2 锚点**、检索到的
状态事实和用户可见回答交给一个独立配置的评审模型，要求逐维返回理由。

边界：

- 硬断言仍由程序执行，判定 PASS/FAIL，不交给评审模型；
- 评审模型只评语义，不改变硬断言结果；
- 评审模型与 producer 分开配置（默认 ``deepseek-chat``，被测模型是
  ``deepseek-flash``）；同一 provider 仍是残余风险，报告必须标注；
- 没有可用评审配置或评审输出不可解析时返回 ``JUDGE_BLOCKED``，
  不生成分数，也不把关键词规则冒充成语义评分；
- P0/P1 的 0 分、硬断言与语义不一致的案例必须进入人工复核队列。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import AGENT_ROOT, Settings
from app.llm.base import ChatMessage, LLMError
from app.llm.openai_compatible import OpenAICompatibleAdapter

from evaluation.phase1.recording import redact

ALLOWED_FAILURE_TAGS = {
    "RETRIEVAL_MISS",
    "CONTEXT_BUDGET",
    "HISTORICAL_EVIDENCE_IGNORED",
    "UNSUPPORTED_CLAIM",
    "MASTERY_OVERCLAIM",
    "GUARD_FALSE_POSITIVE",
    "GUARD_FALSE_NEGATIVE",
    "MODEL_EMPTY_OUTPUT",
    "STREAM_PARSE_ERROR",
    "UNKNOWN",
}

JUDGE_SYSTEM = """你是一个独立的学习 Agent 回答评审员。你只根据给定的状态事实、检索片段和评分锚点评分。

规则：
1. 只评语义质量，不判断 HTTP 状态、调用次数或数据库状态。
2. 每个维度按 0/1/2 打分：2=符合锚点且无越界；1=部分正确但有遗漏/过度推断；0=核心事实错误、编造证据或违反关键限制。
3. 标为 applicable=false 的维度返回 score=null，理由写“不适用”。
4. 不得因为回答里出现关键词就给高分；必须判断事实与任务是否真正完成。
5. 事实正确性以提供的状态事实和检索片段为准；不得用你自己的外部知识替模型补答案。
6. 个人历史只能来自提供的 Evidence/Revision/Understanding；回答中出现未提供的心理过程、外部帮助或学习经历，按编造处理。
7. failureTags 只能从以下集合选择，最多 3 个：RETRIEVAL_MISS, CONTEXT_BUDGET, HISTORICAL_EVIDENCE_IGNORED, UNSUPPORTED_CLAIM, MASTERY_OVERCLAIM, GUARD_FALSE_POSITIVE, GUARD_FALSE_NEGATIVE, MODEL_EMPTY_OUTPUT, STREAM_PARSE_ERROR, UNKNOWN。
8. 只输出一个 JSON 对象，不要 Markdown 代码围栏，不要额外解释。

输出结构：
{"dimensions":[{"id":"...","applicable":true,"score":0,"reason":"<=120字"}],"failureTags":[],"overallNotes":"<=120字"}
"""


@dataclass
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    temperature: float = 0.0
    max_tokens: int = 1200
    timeout_seconds: float = 120.0
    max_attempts: int = 2

    @classmethod
    def from_env(cls) -> "JudgeConfig":
        settings = Settings(_env_file=AGENT_ROOT / ".env")
        return cls(
            model=os.getenv("AILS_PHASE3_JUDGE_MODEL", "deepseek-chat"),
            base_url=os.getenv(
                "AILS_PHASE3_JUDGE_BASE_URL", settings.llm_base_url
            ).rstrip("/"),
            api_key=os.getenv(
                "AILS_PHASE3_JUDGE_API_KEY", settings.llm_api_key
            ),
            temperature=float(os.getenv("AILS_PHASE3_JUDGE_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("AILS_PHASE3_JUDGE_MAX_TOKENS", "1200")),
            timeout_seconds=float(
                os.getenv("AILS_PHASE3_JUDGE_TIMEOUT_SECONDS", "120")
            ),
        )


@dataclass
class JudgeOutcome:
    status: str  # SCORED | JUDGE_BLOCKED
    model: str
    prompt_sha256: str
    prompt_chars: int
    latency_ms: int = 0
    raw_response: str | None = None
    parsed: dict[str, Any] | None = None
    error: str | None = None
    http_status: int | None = None
    usage: dict[str, Any] | None = None
    attempts: int = 0
    settings: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "settings": self.settings,
            "promptSha256": self.prompt_sha256,
            "promptChars": self.prompt_chars,
            "latencyMs": self.latency_ms,
            "rawResponse": self.raw_response,
            "parsed": self.parsed,
            "error": self.error,
            "httpStatus": self.http_status,
            "usage": self.usage,
            "attempts": self.attempts,
        }


def _facts(actual: dict[str, Any]) -> dict[str, Any]:
    context = actual.get("context") or {}
    sections = context.get("sectionTexts") or {}
    keep_prefixes = ("understanding:", "memory:", "trajectory:", "knowledge:")
    compact_sections = [
        {"name": name, "content": str(text)[:500]}
        for name, text in sections.items()
        if str(name).startswith(keep_prefixes)
    ][:12]
    return {
        "run": actual.get("run") or {},
        "retrieval": {
            key: (actual.get("retrieval") or {}).get(key)
            for key in (
                "knowledgeStatus",
                "knowledgeWarning",
                "knowledgeChunkCount",
                "knowledgeDocumentNames",
                "understandingStatuses",
                "understandingConceptIds",
                "memoryIds",
                "memoryContents",
                "evidenceTypes",
                "evidenceCorrectnesses",
                "trajectoryRevisionNos",
                "trajectoryEvidenceIds",
                "historyReviewRequested",
            )
        },
        "contextSections": compact_sections,
        "historyGuard": (actual.get("answer") or {}).get("historyGuard"),
        "evidenceGuard": (actual.get("answer") or {}).get("evidenceGuard"),
        "citations": (actual.get("answer") or {}).get("citations"),
    }


def build_prompt(case, actual: dict[str, Any]) -> str:
    rubric = [
        {
            "id": item.id,
            "description": item.description,
            "score2": item.score2,
            "score1": item.score1,
            "score0": item.score0,
            "applicable": item.applicable,
        }
        for item in case.rubric
    ]
    payload = {
        "caseId": case.id,
        "title": case.title,
        "userQuestion": (actual.get("run") or {}).get("message"),
        "expectedBehavior": list(case.expected_behavior),
        "forbiddenBehavior": list(case.forbidden_behavior),
        "stateAndRetrievalFacts": _facts(actual),
        "answer": str((actual.get("answer") or {}).get("text") or ""),
        "rubric": rubric,
    }
    return (
        JUDGE_SYSTEM
        + "\n\n待评分数据：\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n再次强调：只输出 JSON 对象。"
    )


def _extract_json(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", value, re.DOTALL)
    if match:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("评审输出不是 JSON 对象")


def _validate(parsed: dict[str, Any], case) -> dict[str, Any]:
    expected = {item.id: item for item in case.rubric}
    raw_dims = parsed.get("dimensions")
    if not isinstance(raw_dims, list):
        raise ValueError("缺少 dimensions 数组")
    found: dict[str, dict[str, Any]] = {}
    for raw in raw_dims:
        if not isinstance(raw, dict):
            continue
        dim_id = str(raw.get("id") or "")
        if dim_id not in expected or dim_id in found:
            continue
        criterion = expected[dim_id]
        score = raw.get("score")
        if score is not None:
            if not isinstance(score, int) or score not in (0, 1, 2):
                raise ValueError(f"{dim_id} 的 score 不是 0/1/2/null")
            if not criterion.applicable:
                raise ValueError(f"{dim_id} 标为不适用却给了分数")
        elif criterion.applicable:
            # 允许评审模型显式返回 null 表示“无法判断”，但必须保留为待复核，
            # 不能悄悄当成 0 或 2。
            score = None
        found[dim_id] = {
            "id": dim_id,
            "applicable": criterion.applicable,
            "score": score,
            "reason": redact(str(raw.get("reason") or ""))[:300],
        }
    missing = [dim_id for dim_id in expected if dim_id not in found]
    if missing:
        raise ValueError(f"评审缺少维度：{','.join(missing)}")
    tags = [
        str(tag)
        for tag in parsed.get("failureTags", [])
        if str(tag) in ALLOWED_FAILURE_TAGS
    ][:3]
    return {
        "dimensions": [found[dim_id] for dim_id in expected],
        "failureTags": tags,
        "overallNotes": redact(str(parsed.get("overallNotes") or ""))[:300],
    }


async def judge_case(case, actual: dict[str, Any], config: JudgeConfig | None = None) -> JudgeOutcome:
    config = config or JudgeConfig.from_env()
    prompt = build_prompt(case, actual)
    outcome = JudgeOutcome(
        status="JUDGE_BLOCKED",
        model=config.model,
        settings={
            "temperature": config.temperature,
            "maxTokens": config.max_tokens,
            "timeoutSeconds": config.timeout_seconds,
            "maxAttempts": config.max_attempts,
            "streaming": False,
        },
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_chars=len(prompt),
    )
    if not config.api_key:
        outcome.error = "judge API key 未配置"
        return outcome

    adapter = OpenAICompatibleAdapter(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout_seconds=config.timeout_seconds,
        max_attempts=1,
    )
    started = time.perf_counter()
    try:
        for attempt in range(1, config.max_attempts + 1):
            outcome.attempts = attempt
            try:
                text = await adapter.complete(
                    [
                        ChatMessage(role="system", content=JUDGE_SYSTEM),
                        ChatMessage(role="user", content=prompt),
                    ]
                )
                outcome.raw_response = redact(text)[:6000]
                outcome.parsed = _validate(_extract_json(text), case)
                outcome.status = "SCORED"
                outcome.error = None
                break
            except (LLMError, ValueError, json.JSONDecodeError) as exc:
                outcome.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                if attempt < config.max_attempts:
                    await asyncio.sleep(1.0)
    finally:
        diagnostics = adapter.last_completion_diagnostics
        if diagnostics is not None:
            outcome.http_status = diagnostics.http_status
            outcome.usage = diagnostics.usage
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        await adapter.aclose()
    return outcome


def aggregate_semantics(case, judge: JudgeOutcome) -> dict[str, Any]:
    """把逐维分数聚合成可审计结果；不把平均分当作通过结论。"""
    dimensions = (judge.parsed or {}).get("dimensions") or []
    applicable = [
        item for item in dimensions if item.get("applicable") and item.get("score") is not None
    ]
    scored = [item["score"] for item in applicable]
    zeros = [item["id"] for item in applicable if item["score"] == 0]
    ones = [item["id"] for item in applicable if item["score"] == 1]
    unresolved = [
        item["id"]
        for item in dimensions
        if item.get("applicable") and item.get("score") is None
    ]
    mean = round(sum(scored) / len(scored), 3) if scored else None
    minimum = min(scored) if scored else None
    return {
        "status": judge.status,
        "dimensions": dimensions,
        "applicableCount": len(applicable),
        "scoredCount": len(scored),
        "mean": mean,
        "min": minimum,
        "zeroDimensions": zeros,
        "oneDimensions": ones,
        "unresolvedDimensions": unresolved,
        "failureTags": (judge.parsed or {}).get("failureTags") or [],
        "overallNotes": (judge.parsed or {}).get("overallNotes") or "",
        "judge": judge.as_dict(),
        "requiresHumanReview": bool(zeros or unresolved or judge.status != "SCORED"),
        "thresholds": {
            "zero": "核心事实错误、编造证据或违反关键限制",
            "one": "部分正确但有遗漏/过度推断",
            "two": "正确、完整、无重要越界",
        },
    }
