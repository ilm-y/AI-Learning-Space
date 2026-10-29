"""回答完整性：与「答得对不对」分开的独立状态。

`finish_reason=length` 表示模型输出被 token 预算截断；这时即使用户可见内容
看起来正确，任务也没有完成。历史 B05-r1 / E01-r2 就是这样被判成硬 PASS 的。

状态：

- ``COMPLETE``：拿到 >=10 个用户可见字符，且最后一次调用以 stop/end_turn 结束；
- ``TRUNCATED``：最后一次调用 finish_reason=length；
- ``EMPTY``：没有可用输出（<10 字符），也没有异常；
- ``ERROR``：生成抛异常（可能仍带部分内容）；
- ``UNKNOWN``：有输出但 provider 没给可靠的结束原因，不猜成完整。

这不是语义正确性评分；它只回答「输出有没有结束」。
"""

from __future__ import annotations

from typing import Any

COMPLETE = "COMPLETE"
TRUNCATED = "TRUNCATED"
EMPTY = "EMPTY"
ERROR = "ERROR"
UNKNOWN = "UNKNOWN"

MIN_USABLE_CHARS = 10
_COMPLETE_REASONS = frozenset({"stop", "end_turn", "stop_sequence"})


def evaluate_completion(
    llm: dict[str, Any] | None,
    answer_text: str,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    llm = llm or {}
    text = str(answer_text or "")
    usable = len(text.strip()) >= MIN_USABLE_CHARS
    reasons = [item for item in (llm.get("diagnosticFinishReasons") or []) if item]
    finish_reason = reasons[-1] if reasons else None

    if not usable:
        status = ERROR if error else EMPTY
    elif error:
        status = ERROR
    elif finish_reason == "length":
        status = TRUNCATED
    elif finish_reason in _COMPLETE_REASONS:
        status = COMPLETE
    else:
        status = UNKNOWN

    evidence = f"chars={len(text)},finishReason={finish_reason or 'unknown'}"
    return {
        "status": status,
        "finishReason": finish_reason,
        "chars": len(text),
        "usableChars": usable,
        "callCount": llm.get("callCount"),
        "note": _note(status, finish_reason),
        "evidence": evidence,
    }


def _note(status: str, finish_reason: str | None) -> str:
    if status == TRUNCATED:
        return "finish_reason=length：输出被 token 预算截断，不能判为任务完成。"
    if status == COMPLETE:
        return f"最后一次调用以 {finish_reason} 结束。"
    if status == EMPTY:
        return "没有 >=10 个用户可见字符。"
    if status == ERROR:
        return "生成过程抛出异常，输出可能不完整。"
    return (
        "provider 没有给出可靠 finish_reason；完整性标记为 UNKNOWN，不猜测为完整。"
    )
