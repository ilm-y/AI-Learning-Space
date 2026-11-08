"""真实降级复现：MockLLMAdapter + 真实 Agent 链路 → Resolver DEGRADED。

MockLLMAdapter.complete 返回的是散文而不是 JSON，因此不需要伪造异常：
请求真的走到 Resolver、真的解析失败、真的降级。脚本把日志与状态写到
`tests/eval/reports/degradation-<UTC 时间戳>.log`，供审计引用。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from app.config import Settings
from app.llm.mock import MockLLMAdapter
from app.runtime import AgentRuntime
from app.schemas import ChatRequest
from tests.eval.fake_world import FakeEvalRetriever
from tests.eval.runner import REPORTS_DIR

QUESTION = "连续性的基础是什么？凭什么"


class _Capture(logging.Handler):
    def __init__(self, stream: StringIO) -> None:
        super().__init__(level=logging.INFO)
        self.stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        self.stream.write(self.format(record) + "\n")


async def _run() -> tuple[list[str], dict[str, object], str]:
    buffer = StringIO()
    handler = _Capture(buffer)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    loggers = [
        logging.getLogger("app.retriever.concept_resolver"),
        logging.getLogger("app.runtime"),
    ]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    settings = Settings(
        _env_file=None,
        llm_provider="mock",
        retriever_mode="fixture",
        max_context_tokens=3000,
        concept_resolver_enabled=True,
        concept_resolver_timeout_seconds=5.0,
    )
    runtime = AgentRuntime(settings, MockLLMAdapter())
    request = ChatRequest(
        runId=9001,
        userId=1,
        conversationId=1,
        learningSpaceId=1,
        userMessage=QUESTION,
    )

    prepared = await runtime.prepare(request, retriever=FakeEvalRetriever(1))
    state = prepared.state
    instruction = prepared.context.messages[0].content

    events = [event async for event in runtime.stream(request)]
    stream_events = [event.event for event in events]

    for logger in loggers:
        logger.removeHandler(handler)

    facts: dict[str, object] = {
        "question": QUESTION,
        "resolverStatus": state.resolver_status,
        "resolverWarning": state.resolver_warning,
        "resolverTokenSource": state.resolver_token_source,
        "resolverPromptTokens": state.resolver_prompt_tokens,
        "resolverCompletionTokens": state.resolver_completion_tokens,
        "evidenceStatus": state.evidence_status,
        "evidenceIds": [item.id for item in state.evidences],
        "crossConceptEvidenceStatus": state.cross_concept_evidence_status,
        "contextResolverStatus": prepared.context.resolver_status,
        "contextHasConceptStatus": "本轮概念解析状态" in instruction,
        "contextHasDegradationCaution": "不能据此判断学习者没有相关历史" in instruction,
        "contextClaimsNoHistory": "本次没有检索到任何历史认知记录" in instruction,
        "contextWarnings": prepared.context.warnings,
        "streamEvents": stream_events,
        "streamLastEvent": stream_events[-1] if stream_events else None,
    }
    return stream_events, facts, buffer.getvalue()


def main() -> int:
    stream_events, facts, log_text = asyncio.run(_run())
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = REPORTS_DIR / f"degradation-{stamp}.log"

    ok = (
        facts["resolverStatus"] == "DEGRADED"
        and facts["evidenceStatus"] == "SUCCESS"
        and facts["contextResolverStatus"] == "DEGRADED"
        and facts["contextHasConceptStatus"] is True
        and facts["contextHasDegradationCaution"] is True
        and facts["contextClaimsNoHistory"] is False
        and facts["streamLastEvent"] == "done"
    )

    lines = [
        f"# Resolver 真实降级复现（{datetime.now(timezone.utc).isoformat()}）",
        "# 适配器：MockLLMAdapter（complete 返回散文，不是 JSON）",
        "",
        "## 事实",
        *[f"- {key}: {value}" for key, value in facts.items()],
        "",
        f"## 断言结果：{'PASS' if ok else 'FAIL'}",
        "",
        "## 实际日志",
        log_text.rstrip(),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    print(f"日志文件：{path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
