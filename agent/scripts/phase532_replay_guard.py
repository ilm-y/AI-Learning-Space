"""阶段 5.3.2：把 5.3.1 的真实模型原始回答回放进新的证据约束。

用途：在无法进行新的真实模型调用时，仍然能验证**生产校验器**对已知失败
样本的处置是否符合预期。它不调用 LLM，也不修改任何数据：

1. 通过后端正式读接口重新组装 C（Trajectory-enabled）与 C-only 的上下文；
2. 读取 5.3.1 保留下来的 `rawAnswer`；
3. 调用与 Runtime 完全相同的 `check_history_answer()` / `build_history_fallback()`；
4. 写出 before / issues / after。

注意：`after` 是「如果这次模型输出仍是这段原文，生产链路会返回什么」，
不是一次新的模型生成。B 不属于显式复盘，校验器不介入，因此 before == after。

用法：
    python agent/scripts/phase532_replay_guard.py \
        <summary.json> <summary-clean.json> <old-C.json> <old-C-only.json> <out.json>
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import Settings  # noqa: E402
from app.history_guard import build_history_fallback, check_history_answer  # noqa: E402
from app.llm.mock import MockLLMAdapter  # noqa: E402
from phase53_runtime_eval import (  # noqa: E402
    build_context,
    build_runtime,
    current_only,
    load_summary,
    login,
)


def _record(label: str, prepared, raw: str) -> dict[str, object]:
    issues = check_history_answer(raw, prepared.state)
    fallback = build_history_fallback(prepared.state) if issues else None
    return {
        "case": label,
        "historyReviewRequested": prepared.state.history_review_requested,
        "hasTrajectory": bool(prepared.state.trajectories),
        "evidenceCount": sum(len(item.evidences) for item in prepared.state.trajectories),
        "before": raw,
        "issues": [issue.as_dict() for issue in issues],
        "fallbackUsed": fallback is not None,
        "after": fallback if fallback is not None else raw,
    }


async def main() -> int:
    if len(sys.argv) != 6:
        print(
            "用法：phase532_replay_guard.py <summary.json> <summary-clean.json> "
            "<old-C.json> <old-C-only.json> <out.json>",
            file=sys.stderr,
        )
        return 2
    summary = load_summary(sys.argv[1])
    clean = load_summary(sys.argv[2])
    old_c = load_summary(sys.argv[3])
    old_only = load_summary(sys.argv[4])
    out_path = Path(sys.argv[5])

    settings = Settings()
    runtime, embedding, store = await build_runtime(settings, MockLLMAdapter())
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            token = await login(client, clean["user_c_name"], clean["password"])
            full = await build_context(
                runtime,
                token=token,
                user_id=int(clean["user_c_id"]),
                conversation_id=0,
                space_id=int(clean["space_c_id"]),
                question=clean["questions"]["C"],
                run_id=953200,
            )
            only = current_only(full, settings)
            results = [
                _record("C", full, old_c["call"]["rawAnswer"]),
                _record("C-only", only, old_only["call"]["rawAnswer"]),
            ]
    finally:
        await runtime.aclose()
        await embedding.aclose()
        await store.aclose()

    payload = {
        "note": "回放 5.3.1 真实原始回答，不调用 LLM；after 是生产校验器的处置结果。",
        "outDir": summary.get("out_dir"),
        "results": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出：{out_path}")
    for item in results:
        print(
            f"[{item['case']}] review={item['historyReviewRequested']} "
            f"issues={[issue['kind'] for issue in item['issues']]} "
            f"fallback={item['fallbackUsed']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
