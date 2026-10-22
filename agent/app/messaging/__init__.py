"""Backend ↔ Agent 的 RabbitMQ 消息契约。

这里只放**不依赖 aio_pika** 的纯数据模型与失败分类，方便单测直接导入；
真正的消费者在 `app.messaging.worker`。
"""

from app.messaging.task import (
    AnalysisResultMessage,
    AnalysisResultStatus,
    AnalysisTaskMessage,
    classify_failure,
    retry_count,
)

__all__ = [
    "AnalysisResultMessage",
    "AnalysisResultStatus",
    "AnalysisTaskMessage",
    "classify_failure",
    "retry_count",
]
