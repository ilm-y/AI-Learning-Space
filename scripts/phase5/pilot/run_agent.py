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

import os
import re

import uvicorn
from fastapi import Request

from app.main import app

# Defense in depth：start_services.py 已做启动前断言，这里再确认 Agent 进程
# 实际拿到的 memory 前缀是本次 Pilot 的独占命名，避免绕过启动器直接启动时
# 静默落到共享默认 collection。
_PILOT_MEMORY_PREFIX = re.compile(r"^phase5-pilot-\d{14}-[0-9a-f]{6}-memory$")


def _assert_memory_isolation_env() -> None:
    prefix = os.environ.get("AILS_MEMORY_COLLECTION_PREFIX", "").strip()
    if not _PILOT_MEMORY_PREFIX.match(prefix):
        raise SystemExit(
            "Memory 隔离 fail-closed 断言失败，拒绝启动："
            "AILS_MEMORY_COLLECTION_PREFIX 必须是 "
            "phase5-pilot-<stamp>-memory（当前为空、默认名或格式不符）"
        )


_assert_memory_isolation_env()


def _diagnostic_view(diag) -> dict | None:
    """把 adapter 诊断转成只读视图，并显式标注 Usage 状态。

    生产 `as_dict()` 在 provider 未返回 usage 时给出 `null`。这里只改诊断
    返回的副本：缺失必须写成字符串 `"UNKNOWN"`，不能写成 0，也不能用本地
    估算补齐，否则会伪造 token 证据。
    """
    if diag is None:
        return None
    data = diag.as_dict()
    if data.get("usage"):
        data["usageStatus"] = "REAL"
    else:
        data["usage"] = "UNKNOWN"
        data["usageStatus"] = "UNKNOWN"
    return data


@app.get("/phase5/llm-diagnostics")
async def phase5_llm_diagnostics(request: Request):
    """返回最近一次 LLM 调用的诊断（不含密钥、不含 prompt/正文）。"""
    settings = request.app.state.settings
    runtime = request.app.state.runtime
    adapter = getattr(runtime, "_llm", None)
    stream_diag = getattr(adapter, "last_diagnostics", None)
    completion_diag = getattr(adapter, "last_completion_diagnostics", None)
    stream_view = _diagnostic_view(stream_diag)
    completion_view = _diagnostic_view(completion_diag)
    observed = stream_view is not None or completion_view is not None
    return {
        "provider": settings.llm_provider,
        "model": getattr(adapter, "_model", None),
        "maxTokens": getattr(adapter, "_max_tokens", None),
        "temperature": getattr(adapter, "_temperature", None),
        "retrieverMode": settings.retriever_mode,
        "qdrantCollectionPrefix": settings.qdrant_collection_prefix,
        "conceptResolverEnabled": settings.concept_resolver_enabled,
        "diagnosticScope": "last-call",
        # 当前诊断没有 Spring agent_run.id / trace_id，GET 诊断请求也不携带
        # run_id，因此不能把这条 usage 可靠地挂到某次 AgentRun 上。
        "runIdAssociable": "false" if observed else "unknown",
        "runIdAssociableReason": (
            "诊断只保留最近一次 adapter 调用，未记录 Spring run_id/trace_id；"
            "即使 Agent 请求体带有 runId/traceId，也未写入 adapter 诊断。"
            "串行单请求时只能人工按时间顺序对应，不构成程序化关联。"
        )
        if observed
        else "当前 Agent 进程尚未观察到任何 LLM 调用。",
        "stream": stream_view,
        "completion": completion_view,
    }


if __name__ == "__main__":
    from app.config import Settings

    settings = Settings()
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
