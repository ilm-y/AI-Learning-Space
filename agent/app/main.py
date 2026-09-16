"""Agent 的 HTTP 入口。

三个接口，各有明确用途：

| 接口 | 用途 |
| --- | --- |
| `POST /agent/chat` | 唯一的正式接口：接收一次对话请求，SSE 流式推回回答 |
| `POST /agent/context/preview` | 调试用：只检索与组装，**不调用模型**，把「这次喂了什么给模型」摊开 |
| `GET /health` | 当前生效的是哪套适配器、上下文预算多大 |

preview 不是可有可无的附属品。这个项目最难排查的问题不是「模型回答得不好」，
而是「Agent 到底有没有检索到用户的历史理解」—— 如果只能从最终回答去猜，
每次都要花掉一次模型调用还不一定看得准。preview 把这一步变成可核对的事实。

SSE 事件（Backend 与 Agent 之间的内网契约，与对前端的 message.* 事件刻意不同名）：

```text
meta    { runId, detectedConcepts, estimatedTokens, warnings }
delta   { delta }
done    { runId, detectedConcepts, estimatedTokens, elapsedMs }
error   { runId, message }
```
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import Settings
from app.llm import build_llm_adapter
from app.retriever.base import RetrievalError
from app.runtime import AgentRuntime, StreamEvent
from app.schemas import ChatRequest

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    settings = Settings()
    # LLM 适配器跨请求复用（连接池）；Retriever 每个请求一个，因为要带用户自己的 JWT
    app.state.settings = settings
    app.state.runtime = AgentRuntime(settings, build_llm_adapter(settings))
    log.info("Agent 启动：%s", settings.describe())
    try:
        yield
    finally:
        await app.state.runtime.aclose()


app = FastAPI(title="AI Learning Space — Learning Agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    return {"status": "ok", **settings.describe()}


@app.post("/agent/chat")
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    runtime: AgentRuntime = request.app.state.runtime
    return StreamingResponse(
        _sse(runtime.stream(payload)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/context/preview")
async def preview(payload: ChatRequest, request: Request) -> JSONResponse:
    runtime: AgentRuntime = request.app.state.runtime
    try:
        prepared = await runtime.prepare(payload)
    except RetrievalError as exc:
        return JSONResponse(status_code=502, content={"code": 502, "message": str(exc)})

    return JSONResponse(
        content={
            "code": 0,
            "data": {
                "summary": prepared.state.summary(),
                "retriever": prepared.retriever_name,
                "degraded": prepared.degraded,
                "context": prepared.context.as_dict(),
                "trace": prepared.trace.as_dict(),
            },
        }
    )


async def _sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    async for event in events:
        yield f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
