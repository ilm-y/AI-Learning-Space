"""命令行入口 —— 不起服务、不开后端，直接跑一次完整流水线。

它的价值不是「省一个 HTTP 请求」，而是**把调试循环压缩到最短**：
改一版 prompt 或 Context Builder，不需要重启服务、不需要前端、不需要 MySQL，
一条命令就能看到「检索到了什么 → 组成了什么上下文 → 模型据此怎么答」。

```bash
cd agent
python -m app.cli "TLB Miss 和 Page Fault 有什么关系？"              # 走默认（Mock LLM + Fixture）
python -m app.cli --preview "TLB Miss 呢？"                          # 只看检索与组装，不调用模型
python -m app.cli --token "$JWT" --space 1 "那 TLB Miss 呢？"        # 读真实后端数据
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.llm import build_llm_adapter
from app.retriever.base import RetrievalError
from app.runtime import AgentRuntime
from app.schemas import ChatRequest, HistoryMessage, MessageRole

_SEPARATOR = "─" * 72


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="跑一次 Learning Agent 流水线（默认 Mock LLM + Fixture 数据，可完全离线）",
    )
    parser.add_argument("message", help="用户这一轮说的话")
    parser.add_argument("--preview", action="store_true", help="只检索与组装上下文，不调用模型")
    parser.add_argument("--token", default=None, help="用户 JWT，传了就读真实后端数据")
    parser.add_argument("--user", type=int, default=1, help="用户 ID")
    parser.add_argument("--space", type=int, default=1, help="学习空间 ID")
    parser.add_argument("--conversation", type=int, default=1, help="对话 ID")
    parser.add_argument(
        "--history",
        action="append",
        default=[],
        metavar="ROLE:CONTENT",
        help="历史消息，可重复。ROLE 取 user / assistant，例如 --history user:TLB 是什么",
    )
    return parser.parse_args(argv)


def _parse_history(raw_items: list[str]) -> list[HistoryMessage]:
    history: list[HistoryMessage] = []
    for item in raw_items:
        role, _, content = item.partition(":")
        normalized = role.strip().lower()
        if normalized not in {"user", "assistant"} or not content:
            raise SystemExit(f"--history 格式应为 user:内容 或 assistant:内容，收到：{item}")
        history.append(
            HistoryMessage(
                role=MessageRole.USER if normalized == "user" else MessageRole.ASSISTANT,
                content=content,
            )
        )
    return history


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    runtime = AgentRuntime(settings, build_llm_adapter(settings))
    request = ChatRequest(
        run_id=0,
        user_id=args.user,
        conversation_id=args.conversation,
        learning_space_id=args.space,
        user_message=args.message,
        history=_parse_history(args.history),
        auth_token=args.token,
    )

    try:
        if args.preview:
            prepared = await runtime.prepare(request)
            print(_SEPARATOR)
            print(json.dumps(
                {
                    "summary": prepared.state.summary(),
                    "retriever": prepared.retriever_name,
                    "degraded": prepared.degraded,
                    "context": prepared.context.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ))
            print(_SEPARATOR)
            print(f"trace: {prepared.trace.event_types()}")
            return 0

        print(_SEPARATOR)
        async for event in runtime.stream(request):
            if event.event == "meta":
                print(f"[meta] 命中概念：{event.data['detectedConcepts'] or '（无）'}"
                      f" ｜ 上下文约 {event.data['estimatedTokens']} tokens")
                for warning in event.data.get("warnings") or []:
                    print(f"[warn] {warning}")
                print(_SEPARATOR)
            elif event.event == "delta":
                sys.stdout.write(str(event.data["delta"]))
                sys.stdout.flush()
            elif event.event == "done":
                print(f"\n{_SEPARATOR}")
                print(f"[done] {event.data['elapsedMs']}ms")
            elif event.event == "error":
                print(f"\n[error] {event.data['message']}", file=sys.stderr)
                return 1
        return 0
    except RetrievalError as exc:
        print(f"[error] 检索失败：{exc}", file=sys.stderr)
        return 2
    finally:
        await runtime.aclose()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args(sys.argv[1:]))))


if __name__ == "__main__":
    main()
