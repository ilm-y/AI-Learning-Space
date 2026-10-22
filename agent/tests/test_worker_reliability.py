"""Worker 的可靠性分支：真实 aio-pika 对象用最小 Fake 代替，不连 broker。

覆盖：
- channel 必须带 on_return_raises（mandatory 退回不能被静默吞掉）；
- 未预期异常不能终止整个消费循环；
- FAILED 结果与死信彼此独立，任一成功即可 ACK；
- 重试走「应用重投」：副本确认后才 ACK 原消息；
- 重试耗尽且两边都失败时绝不 ACK（退避后 requeue，连续失败则停机）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.config import Settings
from app.messaging import worker as worker_module
from app.messaging.worker import AnalysisWorker
from app.retriever.base import RetrievalError


class FakeMessage:
    def __init__(
        self,
        body: bytes = b"{}",
        headers: dict[str, Any] | None = None,
        message_id: str = "msg-1",
        correlation_id: str = "corr-1",
        delivery_tag: int = 1,
    ) -> None:
        self.body = body
        self.headers = headers or {}
        self.message_id = message_id
        self.correlation_id = correlation_id
        self.delivery_tag = delivery_tag
        self.content_type = "application/json"
        self.acked = False
        self.nacked: bool | None = None

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = False) -> None:
        self.nacked = requeue


class FakeExchange:
    def __init__(self, fail: Exception | None = None) -> None:
        self.published: list[dict[str, Any]] = []
        self._fail = fail

    async def publish(self, message: Any, routing_key: str, mandatory: bool = False) -> None:
        if self._fail is not None:
            raise self._fail
        self.published.append(
            {"message": message, "routing_key": routing_key, "mandatory": mandatory}
        )


class FakeQueue:
    def __init__(self, messages: list[FakeMessage] | None = None) -> None:
        self._messages = list(messages or [])

    def iterator(self) -> "FakeIterator":
        return FakeIterator(self._messages)

    async def bind(self, exchange: Any, routing_key: str) -> None:
        return None


class FakeIterator:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = messages

    async def __aenter__(self) -> "FakeIterator":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def __aiter__(self) -> "FakeIterator":
        return self

    async def __anext__(self) -> FakeMessage:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeChannel:
    def __init__(self, request_queue: FakeQueue, exchanges: dict[str, FakeExchange]) -> None:
        self.request_queue = request_queue
        self.exchanges = exchanges
        self.qos: int | None = None

    async def set_qos(self, prefetch_count: int) -> None:
        self.qos = prefetch_count

    async def declare_exchange(self, name: str, *args: Any, **kwargs: Any) -> FakeExchange:
        return self.exchanges.setdefault(name, FakeExchange())

    async def declare_queue(self, name: str, *args: Any, **kwargs: Any) -> FakeQueue:
        return self.request_queue


class FakeConnection:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.channel_kwargs: dict[str, Any] | None = None
        self.is_closed = False

    async def channel(self, **kwargs: Any) -> FakeChannel:
        self.channel_kwargs = kwargs
        return self._channel

    async def close(self) -> None:
        self.is_closed = True


class FakeRuntime:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def analyze(self, request: Any) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result

    async def aclose(self) -> None:
        return None


class FakeResult:
    def model_dump(self, by_alias: bool = True, mode: str = "json") -> dict[str, Any]:
        return {"analyzerVersion": "test"}


def settings(**overrides: Any) -> Settings:
    # 退避取 0：单测只关心「失败后是否 requeue / 是否发布副本」，不想真的 sleep。
    return Settings(
        _env_file=None,
        analysis_max_retries=3,
        analysis_publish_failure_backoff_seconds=0,
        **overrides,
    )


def task_body(**overrides: Any) -> bytes:
    payload = {
        "schemaVersion": 1,
        "taskId": 42,
        "runId": 42,
        "traceId": "trace-1",
        "userId": 7,
        "conversationId": 9,
        "learningSpaceId": 3,
        "messageId": 100,
        "assistantMessageId": 101,
        "userMessage": "什么是闭包？",
        "assistantMessage": "闭包是函数与其词法环境的组合。",
        "history": [],
        "session": None,
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def build_worker(
    runtime: FakeRuntime | None = None,
    result_exchange: FakeExchange | None = None,
    dead_exchange: FakeExchange | None = None,
    **settings_overrides: Any,
) -> AnalysisWorker:
    worker = AnalysisWorker(settings(**settings_overrides), runtime or FakeRuntime())
    worker._result_exchange = result_exchange or FakeExchange()
    worker._dead_exchange = dead_exchange or FakeExchange()
    return worker


# ================= R1：mandatory return 必须抛异常 =================


def test_start_enables_on_return_raises(monkeypatch):
    request_queue = FakeQueue()
    exchanges = {
        "learning.analysis.exchange": FakeExchange(),
        "learning.analysis.dlx": FakeExchange(),
    }
    channel = FakeChannel(request_queue, exchanges)
    connection = FakeConnection(channel)

    async def fake_connect(url: str) -> FakeConnection:
        return connection

    monkeypatch.setattr(worker_module.aio_pika, "connect_robust", fake_connect)

    worker = AnalysisWorker(settings(), FakeRuntime())
    asyncio.run(worker.start())

    assert connection.channel_kwargs is not None
    assert connection.channel_kwargs.get("on_return_raises") is True
    assert channel.qos == settings().rabbitmq_prefetch


# ================= R4：未预期异常不能终止消费循环 =================


def test_unexpected_handle_error_is_nacked_and_loop_continues(monkeypatch):
    first, second = FakeMessage(delivery_tag=1), FakeMessage(delivery_tag=2)
    request_queue = FakeQueue([first, second])
    channel = FakeChannel(request_queue, {})
    connection = FakeConnection(channel)

    async def fake_connect(url: str) -> FakeConnection:
        return connection

    monkeypatch.setattr(worker_module.aio_pika, "connect_robust", fake_connect)

    worker = AnalysisWorker(settings(), FakeRuntime())
    handled: list[int] = []

    async def fake_handle(message: FakeMessage) -> None:
        handled.append(message.delivery_tag)
        if message.delivery_tag == 1:
            raise RuntimeError("boom")
        await message.ack()

    worker._handle = fake_handle  # type: ignore[method-assign]
    asyncio.run(worker.start())

    # 第一条被防御性拒绝，第二条照常处理 —— 循环没有整体退出
    assert first.nacked is False
    assert second.acked is True
    assert handled == [1, 2]


# ================= R5：FAILED 结果与死信独立处理 =================


def test_failed_result_published_even_when_dead_letter_fails():
    result_exchange = FakeExchange()
    dead_exchange = FakeExchange(fail=RuntimeError("dlx down"))
    worker = build_worker(result_exchange=result_exchange, dead_exchange=dead_exchange)
    message = FakeMessage()

    asyncio.run(
        worker._fail(
            message, {"taskId": 42, "traceId": "trace-1"},
            "INTERNAL", "boom", retries=0, dead_letter=True,
        )
    )

    assert message.acked is True
    assert message.nacked is None
    assert len(result_exchange.published) == 1
    payload = json.loads(result_exchange.published[0]["message"].body)
    assert payload["status"] == "FAILED"
    assert payload["taskId"] == 42


def test_dead_letter_is_published_even_when_result_publish_fails():
    result_exchange = FakeExchange(fail=RuntimeError("result queue missing"))
    dead_exchange = FakeExchange()
    worker = build_worker(result_exchange=result_exchange, dead_exchange=dead_exchange)
    message = FakeMessage()

    asyncio.run(
        worker._fail(
            message, {"taskId": 42}, "INTERNAL", "boom", retries=0, dead_letter=True,
        )
    )

    assert message.acked is True
    assert len(dead_exchange.published) == 1


def test_both_failures_requeue_the_original():
    worker = build_worker(
        result_exchange=FakeExchange(fail=RuntimeError("result down")),
        dead_exchange=FakeExchange(fail=RuntimeError("dlx down")),
    )
    message = FakeMessage()

    asyncio.run(
        worker._fail(
            message, {"taskId": 42}, "INTERNAL", "boom",
            retries=1, dead_letter=True,
        )
    )

    # 没有任何可靠副本落地：消息必须还给 broker，不能 ACK 丢弃
    assert message.acked is False
    assert message.nacked is True


def test_both_failures_after_retry_exhaustion_are_not_acked():
    worker = build_worker(
        result_exchange=FakeExchange(fail=RuntimeError("result down")),
        dead_exchange=FakeExchange(fail=RuntimeError("dlx down")),
    )
    message = FakeMessage()

    asyncio.run(
        worker._fail(
            message, {"taskId": 42}, "RETRY_EXHAUSTED", "boom",
            retries=3, dead_letter=True,
        )
    )

    # 旧实现会在最后一次尝试时 ACK 丢弃；现在绝不允许：没有死信副本就没有任何留档。
    assert message.acked is False
    assert message.nacked is True


def test_repeated_publish_failures_stop_consumption_without_acking():
    worker = build_worker(
        result_exchange=FakeExchange(fail=RuntimeError("result down")),
        dead_exchange=FakeExchange(fail=RuntimeError("dlx down")),
    )
    worker._consecutive_publish_failures = worker_module.MAX_CONSECUTIVE_PUBLISH_FAILURES - 1
    message = FakeMessage()

    try:
        asyncio.run(
            worker._fail(
                message, {"taskId": 42}, "INTERNAL", "boom",
                retries=1, dead_letter=True,
            )
        )
    except worker_module.WorkerStalledError:
        pass
    else:  # pragma: no cover - 失败路径
        raise AssertionError("连续发布失败达到上限时应停止消费")

    # 消息已经退回队列，停止消费不会丢它
    assert message.nacked is True
    assert message.acked is False


# ================= 单条消息的端到端分支 =================


def test_success_publishes_result_then_acks():
    result_exchange = FakeExchange()
    runtime = FakeRuntime(result=FakeResult())
    worker = build_worker(runtime=runtime, result_exchange=result_exchange)
    message = FakeMessage(body=task_body())

    asyncio.run(worker._handle(message))

    assert message.acked is True
    assert len(result_exchange.published) == 1
    payload = json.loads(result_exchange.published[0]["message"].body)
    assert payload["status"] == "SUCCEEDED"
    assert payload["taskId"] == 42


def test_result_publish_failure_publishes_retry_copy_then_acks():
    dead_exchange = FakeExchange()
    runtime = FakeRuntime(result=FakeResult())
    worker = build_worker(
        runtime=runtime,
        result_exchange=FakeExchange(fail=RuntimeError("result down")),
        dead_exchange=dead_exchange,
    )
    message = FakeMessage(body=task_body())

    asyncio.run(worker._handle(message))

    # 重试副本确认之后才 ACK 原消息；副本带明确的计数
    assert message.acked is True
    assert message.nacked is None
    assert len(dead_exchange.published) == 1
    copy = dead_exchange.published[0]
    assert copy["routing_key"] == settings().analysis_retry_routing_key
    assert copy["message"].headers["x-analysis-retry"] == 1


def test_retry_copy_publish_failure_requeues_original():
    runtime = FakeRuntime(result=FakeResult())
    worker = build_worker(
        runtime=runtime,
        result_exchange=FakeExchange(fail=RuntimeError("result down")),
        dead_exchange=FakeExchange(fail=RuntimeError("retry queue missing")),
    )
    message = FakeMessage(body=task_body())

    asyncio.run(worker._handle(message))

    # 连重试副本都发不出去：原消息必须留在 broker 里
    assert message.acked is False
    assert message.nacked is True


def test_retryable_failure_before_last_attempt_publishes_retry_copy_and_acks():
    result_exchange = FakeExchange()
    dead_exchange = FakeExchange()
    runtime = FakeRuntime(error=RetrievalError("backend down"))
    worker = build_worker(
        runtime=runtime, result_exchange=result_exchange, dead_exchange=dead_exchange
    )
    message = FakeMessage(body=task_body())

    asyncio.run(worker._handle(message))

    # 不发布结果，Spring 侧任务保持 PENDING
    assert result_exchange.published == []
    assert message.acked is True
    assert message.nacked is None
    copy = dead_exchange.published[0]
    assert copy["routing_key"] == settings().analysis_retry_routing_key
    assert copy["message"].headers["x-analysis-retry"] == 1
    assert copy["message"].headers["x-retry-reason"] == "RETRIEVAL_UNAVAILABLE"


def test_retryable_failure_on_last_attempt_publishes_failed_and_acks():
    result_exchange = FakeExchange()
    runtime = FakeRuntime(error=RetrievalError("backend down"))
    worker = build_worker(runtime=runtime, result_exchange=result_exchange)
    message = FakeMessage(
        body=task_body(),
        headers={"x-death": [{"queue": settings().analysis_retry_queue, "count": 2}]},
    )

    asyncio.run(worker._handle(message))

    assert message.acked is True
    payload = json.loads(result_exchange.published[0]["message"].body)
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "RETRIEVAL_UNAVAILABLE"


def test_invalid_task_with_task_id_does_not_crash_the_worker():
    # 回归：这里曾经多传一个位置参数，TypeError 会把整个消费循环带崩。
    result_exchange = FakeExchange()
    dead_exchange = FakeExchange()
    worker = build_worker(result_exchange=result_exchange, dead_exchange=dead_exchange)
    message = FakeMessage(body=json.dumps({"taskId": 42}).encode("utf-8"))

    asyncio.run(worker._handle(message))

    assert message.acked is True
    assert len(dead_exchange.published) == 1
    payload = json.loads(result_exchange.published[0]["message"].body)
    assert payload["status"] == "FAILED"
    assert payload["errorCode"] == "MESSAGE_INVALID"


def test_invalid_json_with_retries_exhausted_is_requeued_not_dropped():
    worker = build_worker(dead_exchange=FakeExchange(fail=RuntimeError("dlx down")))
    message = FakeMessage(
        body=b"not-json",
        headers={"x-analysis-retry": 3},
    )

    asyncio.run(worker._handle(message))

    # 没有 taskId 无法回结果；死信也失败时只能把消息还给 broker。
    # 旧实现在这里 ACK 丢弃 —— 那才是真正的静默丢消息。
    assert message.acked is False
    assert message.nacked is True


def test_explicit_retry_header_takes_precedence_over_x_death():
    result_exchange = FakeExchange()
    dead_exchange = FakeExchange()
    runtime = FakeRuntime(error=RetrievalError("backend down"))
    worker = build_worker(
        runtime=runtime, result_exchange=result_exchange, dead_exchange=dead_exchange
    )
    # x-death 说已重试 2 次，但应用计数是 2 次中的第 2 次（max=3 → 还能再试一次）
    message = FakeMessage(
        body=task_body(),
        headers={
            "x-analysis-retry": 1,
            "x-death": [{"queue": settings().analysis_retry_queue, "count": 9}],
        },
    )

    asyncio.run(worker._handle(message))

    assert message.acked is True
    assert len(dead_exchange.published) == 1
    assert dead_exchange.published[0]["message"].headers["x-analysis-retry"] == 2
    assert result_exchange.published == []


def test_dead_letter_keeps_forensic_identifiers():
    dead_exchange = FakeExchange()
    worker = build_worker(dead_exchange=dead_exchange)
    message = FakeMessage(message_id="msg-9", correlation_id="corr-9")

    asyncio.run(
        worker._fail(
            message, {"taskId": 42}, "INTERNAL", "boom", retries=0, dead_letter=True,
        )
    )

    dead = dead_exchange.published[0]["message"]
    assert dead.message_id == "msg-9"
    assert dead.correlation_id == "corr-9"
    assert dead.headers["x-original-message-id"] == "msg-9"
    assert dead.headers["x-error-code"] == "INTERNAL"
