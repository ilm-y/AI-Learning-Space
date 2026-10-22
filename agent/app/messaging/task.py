"""学习分析任务的 MQ 消息模型与失败分类。

字段与 Java 侧一一对应：

| Python | Java |
| --- | --- |
| `AnalysisTaskMessage` | `com.ails.backend.mq.AnalysisTaskMessage` |
| `AnalysisResultMessage` | `com.ails.backend.mq.AnalysisResultMessage` |

命名沿用项目既有约定：Python 用 snake_case，camelCase 由 `CamelModel` 的 alias
生成器接住，所以两边不需要手工字段映射。
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from pydantic import ValidationError

from app.analysis import AnalysisError
from app.llm.base import LLMError
from app.retriever.base import RetrievalError
from app.schemas import AnalyzeRequest, CamelModel, HistoryMessage, SessionContext

TASK_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1

# Worker 主动重投请求消息时写入的重试计数。Worker 不再依赖 nack + broker DLX
# 转发来重试：它先把带计数的副本确认到 retry 队列，再 ACK 原消息，避免 classic
# 队列在 retry 队列/绑定缺失时静默丢消息。
RETRY_HEADER = "x-analysis-retry"


class AnalysisTaskMessage(CamelModel):
    """Spring Boot → Worker 的分析任务。

    `user_message` / `assistant_message` / `history` 随任务携带，避免 Worker 再发一次
    同步查询；任务里**没有** authToken —— Worker 用服务端凭据回访后端的只读接口。
    """

    schema_version: int = TASK_SCHEMA_VERSION
    task_id: int
    # 与 task_id 相同，单独保留是为了直接构造 AnalyzeRequest
    run_id: int
    trace_id: str | None = None
    user_id: int
    conversation_id: int
    learning_space_id: int
    session_id: int | None = None
    message_id: int | None = None
    assistant_message_id: int | None = None
    user_message: str
    assistant_message: str | None = None
    history: list[HistoryMessage] = []
    session: SessionContext | None = None

    def to_analyze_request(self) -> AnalyzeRequest:
        """转成 HTTP 分析接口用的同一个请求模型。

        `auth_token` 刻意留空：异步链路使用服务端凭据，不携带用户 JWT。
        """
        return AnalyzeRequest(
            runId=self.run_id,
            traceId=self.trace_id,
            userId=self.user_id,
            conversationId=self.conversation_id,
            learningSpaceId=self.learning_space_id,
            messageId=self.message_id,
            userMessage=self.user_message,
            assistantMessage=self.assistant_message,
            history=self.history,
            session=self.session,
            authToken=None,
        )


class AnalysisResultStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AnalysisResultMessage(CamelModel):
    """Worker → Spring Boot 的分析结果。

    成功时带 `result`（`AnalyzeResult.model_dump(by_alias=True)` 的字典）；
    失败时 `result = None`，并给出可排查的错误分类。
    """

    schema_version: int = RESULT_SCHEMA_VERSION
    task_id: int
    trace_id: str | None = None
    message_id: int | None = None
    assistant_message_id: int | None = None
    status: AnalysisResultStatus
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None


# 失败分类：决定这条路是「再试一次」还是「别再浪费模型调用」。
# 值会原样写进后端 agent_run.error_message，运维据此定位。
ERROR_ANALYSIS_INVALID = "ANALYSIS_INVALID"  # 模型输出不可解析 / 不符合契约
ERROR_MESSAGE_INVALID = "MESSAGE_INVALID"  # 请求消息本身不合法
ERROR_RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"  # 后端只读接口不可用
ERROR_LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # 模型上游失败
ERROR_TIMEOUT = "TIMEOUT"
ERROR_RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
ERROR_INTERNAL = "INTERNAL"


def classify_failure(exc: BaseException) -> tuple[str, bool]:
    """把异常映射成 (error_code, retryable)。

    只有环境类故障值得重试；模型输出格式错误 / 请求消息非法是确定性的，
    重放只会得到同一个结果，应该直接落 FAILED 并让后端退回 rules-v1。
    """
    if isinstance(exc, ValidationError):
        return ERROR_MESSAGE_INVALID, False
    if isinstance(exc, AnalysisError):
        return ERROR_ANALYSIS_INVALID, False
    if isinstance(exc, RetrievalError):
        return ERROR_RETRIEVAL_UNAVAILABLE, True
    if isinstance(exc, LLMError):
        return ERROR_LLM_UNAVAILABLE, True
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ERROR_TIMEOUT, True
    return ERROR_INTERNAL, True


def retry_count(headers: dict[str, Any] | None, retry_queue: str) -> int:
    """返回这个任务已经历的重试次数。

    优先读 Worker 自己写入的 `x-analysis-retry`：这是「应用重投」路径的准确计数，
    不依赖 broker 是否记录了 x-death（例如经过其他中间队列时）。若没有这个头
    （旧消息 / 外部使用 nack 拒绝进来的消息），退回到统计 `x-death` 里
    `queue == retry_queue` 的条目；死信到 DLQ 也会写 x-death，不能把它算成重试。
    """
    if not headers:
        return 0
    explicit = headers.get(RETRY_HEADER)
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            pass
    deaths = headers.get("x-death") or []
    total = 0
    for item in deaths:
        if not isinstance(item, dict):
            continue
        if item.get("queue") == retry_queue:
            try:
                total += int(item.get("count", 0))
            except (TypeError, ValueError):
                continue
    return total
