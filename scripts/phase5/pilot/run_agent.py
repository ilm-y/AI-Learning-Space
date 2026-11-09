"""Phase 5 Pilot 专用 Agent 启动器（不修改生产代码）。

在内存中给 FastAPI app 增加一个只读诊断路由，用来读取
`OpenAICompatibleAdapter.last_diagnostics`。Agent 的 SSE `done` 事件没有
usage 字段，若直接从外部观察，Phase 5 要求的 token usage 无法记录。

这个启动器：
- 只 import 生产 `app.main:app`，不修改任何生产模块；
- 只新增 `GET /phase5/llm-diagnostics`，返回适配器已经生成的内存诊断；
- 只绑定 127.0.0.1，进程随 Pilot 结束被清理。
"""

from __future__ import annotations

import uvicorn
from fastapi import Request

from app.main import app


@app.get("/phase5/llm-diagnostics")
async def phase5_llm_diagnostics(request: Request):
    """返回最近一次 LLM 调用的诊断（不含密钥、不含 prompt/正文）。"""
    settings = request.app.state.settings
    runtime = request.app.state.runtime
    adapter = getattr(runtime, "_llm", None)
    stream_diag = getattr(adapter, "last_diagnostics", None)
    completion_diag = getattr(adapter, "last_completion_diagnostics", None)
    return {
        "provider": settings.llm_provider,
        "model": getattr(adapter, "_model", None),
        "maxTokens": getattr(adapter, "_max_tokens", None),
        "temperature": getattr(adapter, "_temperature", None),
        "retrieverMode": settings.retriever_mode,
        "qdrantCollectionPrefix": settings.qdrant_collection_prefix,
        "conceptResolverEnabled": settings.concept_resolver_enabled,
        "stream": stream_diag.as_dict() if stream_diag else None,
        "completion": completion_diag.as_dict() if completion_diag else None,
    }


if __name__ == "__main__":
    from app.config import Settings

    settings = Settings()
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
