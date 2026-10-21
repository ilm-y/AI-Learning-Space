"""Agent 的 HTTP 入口。

四个接口，各有明确用途：

| 接口 | 用途 |
| --- | --- |
| `POST /agent/chat` | 对话：接收一次请求，SSE 流式推回回答 |
| `POST /agent/analyze` | 学习分析：回复结束后抽取证据 / 理解 / 记忆 / 验证决策 |
| `POST /agent/context/preview` | 调试用：只检索与组装，**不调用模型**，把「这次喂了什么给模型」摊开 |
| `GET /health` | 当前生效的是哪套适配器、上下文预算多大 |

Chat 与 Analyze 是 PRD §24 的两个 Agent Internal API。前者生产用户可见的回答，
后者生产**只写库、不展示**的结构化候选 —— 两条链路分开，分析失败不影响已经推给用户的回答。

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

from app.analysis import AnalysisError
from app.config import Settings
from app.llm import build_llm_adapter
from app.llm.base import LLMError
from app.retriever.base import RetrievalError
from app.runtime import AgentRuntime, StreamEvent
from app.schemas import AnalyzeRequest, ChatRequest

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


@app.post("/agent/analyze")
async def analyze(payload: AnalyzeRequest, request: Request) -> JSONResponse:
    runtime: AgentRuntime = request.app.state.runtime
    try:
        result = await runtime.analyze(payload)
    except RetrievalError as exc:
        # 503：后端检索不可用。此时分析没有依据，调用方应退回本地规则而不是当作「没有信号」。
        return JSONResponse(status_code=503, content={"code": 503, "message": str(exc)})
    except (AnalysisError, LLMError) as exc:
        # 502：模型这一跳失败（上游错误 / 输出不可解析）。同样让调用方决定降级。
        return JSONResponse(status_code=502, content={"code": 502, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 —— 分析失败不能把 500 堆栈暴露给后端网关
        log.exception("analyze 未预期失败：run=%s", payload.run_id)
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": f"Agent 内部错误：{type(exc).__name__}"},
        )

    return JSONResponse(content={"code": 0, "data": result.model_dump(by_alias=True)})


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
