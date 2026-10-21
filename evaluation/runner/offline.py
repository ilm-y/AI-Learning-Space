"""离线用例执行器：进程内调用真实 Agent。

不启动 FastAPI、不连后端、不需要 API Key —— 只需要 `agent/` 目录里的代码。
这让评测可以在 CI 里跑，也让「失败」几乎总是代码问题，而不是环境问题。

一次 case 的生命周期：

```text
构造 Settings(fixture + 指定 LLM)
    ↓
AgentRuntime.analyze(AnalyzeRequest)
    ↓
把 AnalyzeResult 摊平成断言友好的 dict
    ↓
按 case.expect 逐条检查
```
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.llm.mock import MockLLMAdapter
from app.runtime import AgentRuntime
from app.schemas import AnalyzeRequest, HistoryMessage, SessionContext

from .checks import CheckOutcome, evaluate
from .models import STATUS_FAILED, STATUS_PASSED, CaseResult, OfflineCase
from .scripted_llm import ScriptedLLMAdapter

# 单条用例的上限。分析器本身没有网络调用，超过这个时间基本是死循环或 fixture 写坏了。
DEFAULT_TIMEOUT_SECONDS = 15.0


async def run_offline_cases(
    cases: list[OfflineCase],
    *,
    fixtures_dir: Path,
    datasets_dir: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    # 顺序执行：Mock/Fixture 都是进程内调用，并行只会让失败日志互相穿插。
    for index, case in enumerate(cases, start=1):
        results.append(
            await _run_one(case, index, fixtures_dir=fixtures_dir, datasets_dir=datasets_dir, timeout=timeout)
        )
    return results


async def _run_one(
    case: OfflineCase,
    index: int,
    *,
    fixtures_dir: Path,
    datasets_dir: Path,
    timeout: float,
) -> CaseResult:
    started = time.perf_counter()
    result = CaseResult(
        case_id=case.id,
        category=case.category,
        mode="offline",
        status=STATUS_FAILED,
        description=case.description,
    )
    runtime: AgentRuntime | None = None
    try:
        settings = _settings_for(case.fixture, fixtures_dir, datasets_dir)
        adapter = (
            MockLLMAdapter()
            if case.engine == "heuristic"
            else ScriptedLLMAdapter(case.llm_response or "")
        )
        runtime = AgentRuntime(settings, adapter)
        request = _build_request(case.request, index)

        try:
            analyzed = await asyncio.wait_for(runtime.analyze(request), timeout=timeout)
            error: BaseException | None = None
        except Exception as exc:  # noqa: BLE001 —— 用例要能断言「这里应该失败」
            analyzed = None
            error = exc

        if case.expect_error is not None:
            result.checks = [_error_check(case.expect_error, error)]
        elif error is not None:
            result.error = f"{type(error).__name__}: {error}"
            return result
        else:
            actual = _actual(analyzed)
            result.detail = {"actual": actual}
            result.checks = evaluate(case.expect, actual)

        if not result.checks:
            # 空断言集会让 all([]) 为 True，用例「什么都没验」却判通过。
            # live 执行器已有同样的保护，这里保持两边一致。
            result.error = "用例没有任何断言 —— 请提供 expect 或 expectError"
            return result

        result.status = STATUS_PASSED if all(check.passed for check in result.checks) else STATUS_FAILED
        return result
    except Exception as exc:  # noqa: BLE001 —— 加载 fixture / 构造请求失败也要落到报告里
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if runtime is not None:
            try:
                await runtime.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不该覆盖用例结论
                pass


# ===================== 输入构造 =====================


def _settings_for(fixture: str, fixtures_dir: Path, datasets_dir: Path) -> Settings:
    """找到 fixture 文件。优先 agent/fixtures（与开发/测试同一份数据），再找 datasets。"""
    candidate = fixtures_dir / fixture
    if not candidate.exists():
        candidate = datasets_dir / fixture
    if not candidate.exists():
        raise FileNotFoundError(f"找不到 fixture：{fixture}（已查 {fixtures_dir} 与 {datasets_dir}）")
    return Settings(
        _env_file=None,  # 屏蔽开发机 .env：评测结果不能随本机密钥变化
        retriever_mode="fixture",
        llm_provider="mock",
        fixture_path=str(candidate),
        max_context_tokens=3000,
        recent_history_limit=12,
    )


def _build_request(raw: dict[str, Any], index: int) -> AnalyzeRequest:
    session_raw = raw.get("session")
    session = SessionContext.model_validate(session_raw) if isinstance(session_raw, dict) else None
    history = [
        HistoryMessage(role=item["role"], content=item.get("content", ""))
        for item in raw.get("history", [])
    ]
    return AnalyzeRequest(
        run_id=raw.get("runId", index),
        trace_id=raw.get("traceId", f"eval-{index:04d}"),
        user_id=raw.get("userId", 1),
        conversation_id=raw.get("conversationId", 100),
        learning_space_id=raw.get("learningSpaceId", 1),
        message_id=raw.get("messageId", 9000 + index),
        user_message=raw.get("userMessage", ""),
        assistant_message=raw.get("assistantMessage"),
        history=history,
        session=session,
        auth_token=None,
    )


# ===================== 输出摊平 =====================


def _actual(result) -> dict[str, Any]:
    """把 AnalyzeResult 转成断言用的扁平结构。

    这里刻意不复用 `model_dump()`：用例作者关心的是「几条、什么类型、挂在哪个概念」，
    而不是 Jackson/Pydantic 的字段嵌套。转换逻辑集中在这里，新增字段时只改一处。
    """
    evidences = result.evidence_candidates
    memories = result.memory_candidates
    proposal = result.understanding_proposal
    decision = result.verification_decision

    actual: dict[str, Any] = {
        "detectedConcepts": list(result.detected_concepts),
        "analyzerVersion": result.analyzer_version,
        "evidence": {
            "count": len(evidences),
            "conceptIds": [item.concept_id for item in evidences],
            "types": [item.evidence_type.value for item in evidences],
            "correctness": [item.correctness.value for item in evidences],
            "observations": [item.observation for item in evidences],
            "needFollowUp": [item.need_follow_up for item in evidences],
            "reasoningQuality": [item.reasoning_quality for item in evidences],
            "specificity": [item.specificity for item in evidences],
            "aiConfidence": [item.ai_confidence for item in evidences],
        },
        "memory": {
            "count": len(memories),
            "scopes": [item.scope_type.value for item in memories],
            "conceptIds": [item.concept_id for item in memories],
            "spaceIds": [item.learning_space_id for item in memories],
            "types": [item.memory_type.value for item in memories],
            "keys": [item.memory_key for item in memories],
            "contents": [item.content for item in memories],
            "actions": [item.action.value for item in memories],
            "importance": [item.importance for item in memories],
        },
        "verification": {
            "action": decision.action.value,
            "trigger": decision.trigger.value if decision.trigger else None,
            "conceptId": decision.concept_id,
            "questionType": decision.question_type.value if decision.question_type else None,
            "questionText": decision.question_text,
            "reason": decision.reason,
        },
        "proposal": {
            "present": proposal is not None,
            "conceptId": proposal.concept_id if proposal else None,
            "knownParts": list(proposal.known_parts) if proposal else [],
            "uncertainParts": list(proposal.uncertain_parts) if proposal else [],
            "misconceptions": list(proposal.misconceptions) if proposal else [],
            "latestUserExplanation": proposal.latest_user_explanation if proposal else None,
            # 派生字段：同一个路径只能挂一条断言，截断用例需要同时看长度与结尾
            "explanationLength": len(proposal.latest_user_explanation or "") if proposal else 0,
            "explanationTruncated": bool(
                proposal and (proposal.latest_user_explanation or "").endswith("…")
            ),
        },
    }
    return actual


def _error_check(expect: dict[str, Any], error: BaseException | None) -> CheckOutcome:
    expected_type = expect.get("type", "Exception")
    expected_text = expect.get("contains")
    if error is None:
        return CheckOutcome(
            path="error",
            op="raises",
            expected=f"{expected_type}({expected_text or ''})",
            actual=None,
            passed=False,
            message="期望抛出异常，但分析成功返回",
        )
    builtin_type = _builtin_type(expected_type)
    type_ok = type(error).__name__ == expected_type or (
        builtin_type is not None and isinstance(error, builtin_type)
    )
    text = str(error)
    text_ok = expected_text is None or expected_text in text
    return CheckOutcome(
        path="error",
        op="raises",
        expected=f"{expected_type}({expected_text or ''})",
        actual=f"{type(error).__name__}: {text}",
        passed=type_ok and text_ok,
        message="" if type_ok and text_ok else f"类型匹配={type_ok} 文本匹配={text_ok}",
    )


def _builtin_type(name: str) -> type | None:
    """只认识 Python 内置异常名。

    不用 eval/importlib 动态查找：用例文件是数据，不应具备导入任意模块的能力。
    自定义异常（如 AnalysisError）按类名精确匹配；名字写错时不会退化成
    「任何 Exception 都算匹配」，否则 `expectError` 会形同虚设。
    """
    builtins = {cls.__name__: cls for cls in (Exception, ValueError, RuntimeError, TypeError)}
    return builtins.get(name)
