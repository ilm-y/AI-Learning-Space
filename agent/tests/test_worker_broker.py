"""真实 RabbitMQ Broker 下的 Worker 可靠性验证。

需要显式提供 broker：

```bash
AILS_TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m pytest -q tests/test_worker_broker.py
```

没有设置变量时整组跳过 —— 跳过不等于通过。这些用例验证的是只有真实 broker
才能回答的问题：重试副本是否真的经 TTL 回到请求队列，以及「连副本都发不出去」
时原消息是否真的还在队列里（而不是被 ACK 丢掉）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import aio_pika
import pytest

from app.config import Settings
from app.messaging.worker import AnalysisWorker

BROKER_URL = os.getenv("AILS_TEST_RABBITMQ_URL")

pytestmark = pytest.mark.skipif(
    not BROKER_URL,
    reason="未设置 AILS_TEST_RABBITMQ_URL；真实 Broker 测试未执行（跳过 ≠ 通过）",
)

BODY = b'{"schemaVersion":1,"taskId":42}'


class FakeRuntime:
    async def analyze(self, request: Any) -> Any:  # pragma: no cover - 本文件不跑分析
        raise AssertionError("真实 Broker 用例不应调用模型")

    async def aclose(self) -> None:
        return None


def settings_for_test() -> Settings:
    """每次用独立拓扑，避免和开发环境的队列互相干扰。"""
    suffix = uuid.uuid4().hex[:8]
    return Settings(
        _env_file=None,
        rabbitmq_url=BROKER_URL,
        rabbitmq_prefetch=1,
        analysis_exchange=f"t.exchange.{suffix}",
        analysis_dlx=f"t.dlx.{suffix}",
        analysis_request_queue=f"t.request.{suffix}",
        analysis_request_routing_key=f"t.request.rk.{suffix}",
        analysis_result_queue=f"t.result.{suffix}",
        analysis_result_routing_key=f"t.result.rk.{suffix}",
        analysis_retry_queue=f"t.retry.{suffix}",
        analysis_retry_routing_key=f"t.retry.rk.{suffix}",
        analysis_dead_queue=f"t.dlq.{suffix}",
        analysis_dead_routing_key=f"t.dead.rk.{suffix}",
        analysis_retry_ttl_ms=300,
        analysis_max_retries=3,
        analysis_publish_failure_backoff_seconds=0,
    )


async def _publish_request(worker: AnalysisWorker, settings: Settings) -> None:
    assert worker._result_exchange is not None
    await worker._result_exchange.publish(
        aio_pika.Message(
            body=BODY,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id="request-1",
        ),
        routing_key=settings.analysis_request_routing_key,
        mandatory=True,
    )


async def _get_within(queue: Any, *, timeout: float, no_ack: bool = True) -> Any:
    """在 deadline 内轮询取消息。

    aio-pika 的 ``Queue.get(timeout=...)`` 只给单次 ``basic.get`` 的 RPC 设超时，
    队列为空会立刻返回 GetEmpty，并不会等待消息出现。TTL 队列的回流必须自己轮询。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        message = await queue.get(no_ack=no_ack, fail=False)
        if message is not None:
            return message
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _assert_queue_empty(queue_name: str) -> None:
    connection = await aio_pika.connect_robust(BROKER_URL)
    try:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True, passive=True)
        leftover = await queue.get(no_ack=True, fail=False)
        assert leftover is None, "原消息没有被 ACK：连接关闭后它又回到了队列"
    finally:
        await connection.close()


def test_retry_copy_returns_to_request_queue_and_original_is_acked() -> None:
    async def scenario() -> None:
        settings = settings_for_test()
        worker = AnalysisWorker(settings, FakeRuntime())
        await worker.connect()
        try:
            assert worker._request_queue is not None
            await _publish_request(worker, settings)
            incoming = await worker._request_queue.get(no_ack=False, timeout=5)
            assert incoming is not None

            # 发布重试副本 → 副本确认 → ACK 原消息
            await worker._retry_later(incoming, 1, reason="RETRIEVAL_UNAVAILABLE")

            # TTL 到期后副本回到请求队列，应用计数头原样保留
            retried = await _get_within(worker._request_queue, timeout=5)
            assert retried is not None, "重试副本没有回到请求队列"
            assert retried.body == BODY
            assert retried.headers["x-analysis-retry"] == 1
            assert retried.headers["x-original-message-id"] == "request-1"
        finally:
            await worker.stop()

        # 原消息已 ACK，否则连接关闭时它会重新入队
        await _assert_queue_empty(settings.analysis_request_queue)

    asyncio.run(scenario())


def test_when_no_copy_can_be_published_the_message_stays_in_the_queue() -> None:
    async def scenario() -> None:
        settings = settings_for_test()
        worker = AnalysisWorker(settings, FakeRuntime())
        await worker.connect()
        try:
            assert worker._request_queue is not None
            await _publish_request(worker, settings)
            incoming = await worker._request_queue.get(no_ack=False, timeout=5)
            assert incoming is not None

            async def unavailable(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("simulated broker outage")

            # 模拟「FAILED 结果与死信都发不出去」：真实 broker 仍然在线，
            # 因此可以观察 _fail 的最终动作到底把消息留在了哪里。
            worker._publish_result = unavailable  # type: ignore[method-assign]
            worker._dead_letter = unavailable  # type: ignore[method-assign]

            await worker._fail(
                incoming,
                {"taskId": 42},
                "RETRY_EXHAUSTED",
                "boom",
                retries=3,
                dead_letter=True,
            )

            # 消息没有被 ACK 丢弃：退避后 nack(requeue=True)，仍然可以被消费
            again = await _get_within(worker._request_queue, timeout=5)
            assert again is not None, "两边都失败时消息被丢弃了"
            assert again.body == BODY
        finally:
            await worker.stop()

    asyncio.run(scenario())
