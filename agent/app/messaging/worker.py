"""RabbitMQ 分析 Worker —— 消费任务、调用分析链路、回传结果。

```text
learning.analysis.request
        ↓  (手动 ACK：结果发布成功之后才确认)
AgentRuntime.analyze()
        ↓
learning.analysis.result
```

<h3>可靠性约定</h3>
<ul>
  <li><b>手动 ACK。</b> 结果消息<b>发布确认之后</b>才 ack 请求消息。分析成功但结果没发出去，
      请求会被重新投递，而不是两条都丢。</li>
  <li><b>发布确认。</b> result exchange 使用 publisher confirms（aio-pika 默认开启）；
      不可路由（mandatory）也会抛异常。</li>
  <li><b>应用负责重投。</b> 可重试故障时 Worker 自己把带 `x-analysis-retry` 计数的副本
      发布到 retry 队列（confirm + mandatory 都成功），然后才 ACK 原消息。不依赖
      `nack` + 请求队列 DLX 的 broker 内转发 —— classic 队列在 retry 队列或绑定缺失时
      会静默丢弃消息。超过上限不再调用模型。</li>
  <li><b>死信。</b> 永久错误（消息格式不对 / 模型输出不可解析）与重试耗尽的队列消息进入
      `learning.analysis.dlq`，同时回一条 FAILED 结果，让 Spring 侧能把任务落到终态。</li>
  <li><b>不静默丢弃。</b> FAILED 结果与死信都没发出去时绝不 ACK：退避后 nack(requeue=True)，
      把消息留在 broker 里。连续失败到上限就停止消费并退出，等运维/进程守护恢复，
      消息仍在队列中。</li>
  <li><b>幂等。</b> 消息可能被重复投递；后端的 Evidence dedupe 与 Revision「内容不变不落版本」
      保证重复处理不会重复记账。</li>
</ul>

用法：

```bash
cd agent
python -m app.worker
```

没有 RabbitMQ 时进程会连接失败并退出；HTTP 服务 `python -m app.main` 不受影响。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractRobustConnection

from app.config import Settings
from app.llm import build_llm_adapter
from app.messaging.task import (
    ERROR_INTERNAL,
    ERROR_MESSAGE_INVALID,
    ERROR_RETRY_EXHAUSTED,
    RETRY_HEADER,
    TASK_SCHEMA_VERSION,
    AnalysisResultMessage,
    AnalysisResultStatus,
    AnalysisTaskMessage,
    classify_failure,
    retry_count,
)
from app.runtime import AgentRuntime

log = logging.getLogger(__name__)

# 连续多少次「连副本都发不出去」后停止消费。此时消息已经 nack(requeue=True) 留在队列里，
# 进程退出（非零）让守护进程重启；重启时会重新声明拓扑，恢复后消息自然被重新消费。
MAX_CONSECUTIVE_PUBLISH_FAILURES = 10


class WorkerStalledError(RuntimeError):
    """连续无法向 broker 持久化任何副本：停止消费，保留消息等待运维恢复。"""


class AnalysisWorker:
    """一个 Worker 实例：一条连接、一个 channel、prefetch 个并发。"""

    def __init__(self, settings: Settings, runtime: AgentRuntime) -> None:
        self._settings = settings
        self._runtime = runtime
        self._connection: AbstractRobustConnection | None = None
        self._request_queue: AbstractQueue | None = None
        self._result_exchange: aio_pika.abc.AbstractExchange | None = None
        self._dead_exchange: aio_pika.abc.AbstractExchange | None = None
        # 连续「无法发布任何副本」的次数；任何一次发布成功都会清零。
        self._consecutive_publish_failures = 0

    async def start(self) -> None:
        await self.connect()
        await self.consume()

    async def connect(self) -> None:
        """连接并声明拓扑。拆成独立方法，单测可以不进入消费循环。"""
        settings = self._settings
        self._connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        # on_return_raises 不是可选项：mandatory 只让 broker 把不可路由的消息退回，
        # 默认（False）时 aio-pika 只记一条日志，publish 仍会正常返回、请求消息随后被 ACK，
        # 结果就此丢失而 Spring 侧任务永远停在 PENDING。打开后退回会变成 PublishError，
        # 走 nack → retry 队列，最终由重试耗尽分支落 FAILED 并进死信。
        channel = await self._connection.channel(on_return_raises=True)
        await channel.set_qos(prefetch_count=settings.rabbitmq_prefetch)

        # 拓扑声明是幂等的：后端可能已经建过，两边参数必须一致，否则 broker 会
        # 报 PRECONDITION_FAILED —— 这也是「改了 TTL 却没删队列」最常见的启动失败原因。
        exchange = await channel.declare_exchange(
            settings.analysis_exchange, ExchangeType.DIRECT, durable=True
        )
        dead_exchange = await channel.declare_exchange(
            settings.analysis_dlx, ExchangeType.DIRECT, durable=True
        )
        request_queue = await channel.declare_queue(
            settings.analysis_request_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": settings.analysis_dlx,
                "x-dead-letter-routing-key": settings.analysis_retry_routing_key,
            },
        )
        await request_queue.bind(exchange, routing_key=settings.analysis_request_routing_key)

        retry_queue = await channel.declare_queue(
            settings.analysis_retry_queue,
            durable=True,
            arguments={
                "x-message-ttl": settings.analysis_retry_ttl_ms,
                "x-dead-letter-exchange": settings.analysis_exchange,
                "x-dead-letter-routing-key": settings.analysis_request_routing_key,
            },
        )
        await retry_queue.bind(dead_exchange, routing_key=settings.analysis_retry_routing_key)

        # 参数必须与后端 RabbitMqConfig 完全一致：结果队列挂了 DLX，
        # 无法解析 / 永久失败的结果由 backend 拒绝后进入死信。
        # 少写 x-dead-letter-* 会得到 PRECONDITION_FAILED，Worker 直接起不来。
        result_queue = await channel.declare_queue(
            settings.analysis_result_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": settings.analysis_dlx,
                "x-dead-letter-routing-key": settings.analysis_dead_routing_key,
            },
        )
        await result_queue.bind(exchange, routing_key=settings.analysis_result_routing_key)

        dead_queue = await channel.declare_queue(settings.analysis_dead_queue, durable=True)
        await dead_queue.bind(dead_exchange, routing_key=settings.analysis_dead_routing_key)

        self._request_queue = request_queue
        self._result_exchange = exchange
        self._dead_exchange = dead_exchange

        log.info(
            "分析 Worker 已启动：queue=%s prefetch=%d retries<=%d",
            settings.analysis_request_queue,
            settings.rabbitmq_prefetch,
            settings.analysis_max_retries,
        )

    async def consume(self) -> None:
        """持续消费请求队列，直到迭代器结束或连续发布失败触发停机。"""
        assert self._request_queue is not None
        async with self._request_queue.iterator() as iterator:
            async for message in iterator:
                # 最后一道防线：_handle 里任何没被预期的异常（例如 ack 时连接已断、
                # 结果 payload 序列化失败）都不能让 `async for` 整体退出 —— 那会关闭
                # iterator 并留下一条既没 ack 也没 nack 的消息，只能等连接断开才重投。
                # 这里统一拒绝到 retry 队列，保持「失败重试」的既有语义。
                try:
                    await self._handle(message)
                except WorkerStalledError:
                    # 消息已经在 _backoff_and_requeue 里退回队列；继续消费只会反复失败。
                    # 抛出去让进程非零退出，由进程守护重启并重新声明拓扑。
                    log.error("连续发布失败达到上限，停止消费分析任务")
                    raise
                except Exception:
                    log.exception(
                        "处理分析任务时发生未捕获异常, deliveryTag=%s",
                        message.delivery_tag,
                    )
                    try:
                        await message.nack(requeue=False)
                    except Exception:
                        log.exception("拒绝分析任务失败，等待 broker 重新投递")

    async def stop(self) -> None:
        await self._runtime.aclose()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()

    # ================= 单条消息 =================

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        retries = retry_count(message.headers, self._settings.analysis_retry_queue)
        try:
            raw = json.loads(message.body)
        except Exception:
            log.exception("分析任务不是合法 JSON，转入死信")
            # 没有 taskId，只能死信；死信本身失败时 _fail 会退回消息，绝不 ACK 丢弃。
            await self._fail(
                message, {}, ERROR_MESSAGE_INVALID, "分析任务不是合法 JSON",
                retries=retries, dead_letter=True,
            )
            return

        if not isinstance(raw, dict):
            log.error("分析任务不是 JSON 对象，转入死信")
            await self._fail(
                message, {}, ERROR_MESSAGE_INVALID, "分析任务不是 JSON 对象",
                retries=retries, dead_letter=True,
            )
            return

        task_id = _as_int(raw.get("taskId", raw.get("task_id")))
        try:
            task = AnalysisTaskMessage.model_validate(raw)
        except Exception as exc:
            # 请求本身不合法：即使拿到 taskId 也无法可靠分析，只能回一条 FAILED
            # 让 Spring 把任务落终态，然后进死信供排查。
            # 注意这里曾经多传了一个位置参数（task_id），会在契约错误时抛
            # TypeError 并把整个消费循环带崩。
            log.warning("分析任务不符合契约, taskId=%s: %s", task_id, exc)
            await self._fail(
                message, raw if isinstance(raw, dict) else {},
                ERROR_MESSAGE_INVALID, str(exc),
                retries=retries, dead_letter=True,
            )
            return

        if task.schema_version != TASK_SCHEMA_VERSION:
            await self._fail(
                message, task,
                "TASK_SCHEMA_UNSUPPORTED",
                f"不支持的 schemaVersion={task.schema_version}",
                retries=retries,
                dead_letter=True,
            )
            return

        if retries >= self._settings.analysis_max_retries:
            # 计数在调用模型之前就检查：已耗尽的副本不能再触发一次模型调用。
            log.error("分析任务重试耗尽, taskId=%s, retries=%d", task.task_id, retries)
            await self._fail(
                message, task,
                ERROR_RETRY_EXHAUSTED,
                f"重试 {retries} 次后仍失败",
                retries=retries,
                dead_letter=True,
            )
            return

        started = time.monotonic()
        try:
            result = await self._runtime.analyze(task.to_analyze_request())
        except Exception as exc:
            error_code, retryable = classify_failure(exc)
            log.warning(
                "分析失败, taskId=%s, retries=%d, code=%s, retryable=%s: %s",
                task.task_id, retries, error_code, retryable, exc,
            )
            if retryable and retries + 1 < self._settings.analysis_max_retries:
                # 不发布结果：Spring 侧任务保持 PENDING，等 retry 队列到期后再试。
                # 副本确认之后才 ACK 原消息；副本发不出去就退避重投，绝不丢。
                await self._retry_later(message, retries + 1, reason=error_code)
                return
            # 走到这里有两种情况：可重试错误但这是最后一次尝试，或永久错误。
            # FAILED 结果与死信都发不出去时由 _fail 退避重投，不会 ACK 丢弃。
            await self._fail(
                message, task, error_code, str(exc),
                retries=retries + 1 if retryable else retries,
                dead_letter=True,
            )
            return

        payload = AnalysisResultMessage(
            task_id=task.task_id,
            trace_id=task.trace_id,
            message_id=task.message_id,
            assistant_message_id=task.assistant_message_id,
            status=AnalysisResultStatus.SUCCEEDED,
            result=result.model_dump(by_alias=True, mode="json"),
        )
        try:
            await self._publish_result(payload, task.task_id)
        except Exception as exc:
            # 结果没发出去就不能 ack：复制一份带计数的重试消息，下一轮重新分析。
            log.exception("发布分析结果失败, taskId=%s", task.task_id)
            if retries + 1 < self._settings.analysis_max_retries:
                await self._retry_later(message, retries + 1, reason=ERROR_INTERNAL)
                return
            # 最后一次也没发出去：只能走 FAILED 结果 + 死信；两者都失败时 _fail 负责保留消息。
            await self._fail(
                message, task, ERROR_INTERNAL, f"分析结果发布失败: {exc}",
                retries=retries + 1, dead_letter=True,
            )
            return

        await message.ack()
        log.info(
            "分析完成, taskId=%s, traceId=%s, elapsedMs=%d",
            task.task_id, task.trace_id, int((time.monotonic() - started) * 1000),
        )

    async def _fail(
        self,
        message: AbstractIncomingMessage,
        task: AnalysisTaskMessage | dict[str, Any],
        error_code: str,
        error_message: str,
        *,
        retries: int,
        dead_letter: bool,
    ) -> None:
        """发布 FAILED 结果；可选择同时把原消息转入死信。

        两个投递动作彼此独立：结果发不出去不应阻止死信留档，反之亦然。
        只要有一边成功，这条请求消息就可以确认 —— 另一边的丢失已经被记进日志。

        **两边都失败时绝不 ACK。** 信息只存在于 broker 里，ACK 就等于静默丢弃。
        此时退避后把消息退回队列；连续失败到上限则停止消费（消息仍在队列中），
        由运维或进程守护恢复后继续。数据库/队列恢复后，失败任务自然会再次走这条路径。
        """
        task_id = task.task_id if isinstance(task, AnalysisTaskMessage) else _as_int(task.get("taskId"))
        trace_id = task.trace_id if isinstance(task, AnalysisTaskMessage) else task.get("traceId")

        result_published = False
        dead_lettered = False
        if task_id is not None:
            # AnalysisResultMessage.task_id 是必填 int。非法 JSON / 没有 taskId 的消息
            # 只能进死信，不能在这里构造结果 —— 否则 ValidationError 会把 _fail 自己打崩，
            # 消息永远到不了死信（外层守卫只会不停 nack）。
            payload = AnalysisResultMessage(
                task_id=task_id,
                trace_id=trace_id,
                message_id=(
                    task.message_id if isinstance(task, AnalysisTaskMessage)
                    else _as_int(task.get("messageId"))
                ),
                assistant_message_id=(
                    task.assistant_message_id if isinstance(task, AnalysisTaskMessage)
                    else _as_int(task.get("assistantMessageId"))
                ),
                status=AnalysisResultStatus.FAILED,
                result=None,
                error_code=error_code,
                error_message=error_message,
                retryable=False,
            )
            try:
                await self._publish_result(payload, task_id)
                result_published = True
            except Exception:
                log.exception("发布失败结果失败, taskId=%s, code=%s", task_id, error_code)
        if dead_letter:
            try:
                await self._dead_letter(message, error_code=error_code, retries=retries)
                dead_lettered = True
            except Exception:
                log.exception("投递死信失败, taskId=%s, code=%s", task_id, error_code)

        if result_published or dead_lettered:
            await message.ack()
            return
        # 没有任何可靠副本落地：消息只能留在 broker 里。
        log.error(
            "失败结果与死信均未投递成功，请求消息退回队列（不 ACK）, taskId=%s, code=%s, messageId=%s",
            task_id, error_code, message.message_id,
        )
        await self._backoff_and_requeue(message)

    # ================= 队列操作 =================

    async def _retry_later(
        self, message: AbstractIncomingMessage, retries: int, *, reason: str
    ) -> None:
        """把原消息替换成一份带重试计数的副本，副本确认之后才 ACK 原消息。

        与 `nack(requeue=False)` + broker DLX 转发的区别：这里能感知「retry 队列不存在 /
        绑定缺失」—— mandatory 退回会抛异常，此时保留原消息并退避重投，而不是让 broker
        把消息转发到一个不存在的目的地后静默丢弃。
        """
        try:
            await self._publish_request_retry(message, retries, reason=reason)
        except Exception:
            log.exception(
                "投递分析任务重试副本失败，保留原消息等待重投, retries=%d", retries
            )
            await self._backoff_and_requeue(message)
            return
        await message.ack()

    async def _publish_request_retry(
        self, message: AbstractIncomingMessage, retries: int, *, reason: str
    ) -> None:
        """把请求消息发布到 retry 队列；confirm + mandatory 由 aio-pika 保证。"""
        assert self._dead_exchange is not None
        # 重新构造 header，不复制 x-death：重试计数由 Worker 自己维护，broker 的
        # 死信历史只描述「这条消息曾经进过哪个队列」，两者混在一起会重复计数。
        headers: dict[str, Any] = {
            "x-original-queue": self._settings.analysis_request_queue,
            RETRY_HEADER: retries,
            "x-retry-reason": reason,
        }
        if message.message_id:
            headers["x-original-message-id"] = message.message_id
        await self._dead_exchange.publish(
            Message(
                body=message.body,
                content_type=message.content_type or "application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                headers=headers,
            ),
            routing_key=self._settings.analysis_retry_routing_key,
            mandatory=True,
        )
        self._consecutive_publish_failures = 0

    async def _backoff_and_requeue(self, message: AbstractIncomingMessage) -> None:
        """退避后把消息退回原队列。

        只有「连副本都发不出去」才会走到这里，通常意味着 broker 或拓扑出了基础设施
        问题。此时 requeue 是唯一不丢消息的选择；退避用于避免 broker 半可用状态下的
        快速空转。连续失败到上限后抛 {@link WorkerStalledError} 停止消费——消息已经
        退回队列，进程退出不会丢它。
        """
        self._consecutive_publish_failures += 1
        await self._sleep_backoff()
        try:
            await message.nack(requeue=True)
        except Exception:
            # nack 发不出去时消息仍是 unacked；连接断开后 broker 会自动重新入队。
            log.exception("退回分析任务失败，等待连接恢复后由 broker 重新投递")
        if self._consecutive_publish_failures >= MAX_CONSECUTIVE_PUBLISH_FAILURES:
            raise WorkerStalledError(
                f"连续 {self._consecutive_publish_failures} 次无法向 broker 发布任何副本"
            )

    async def _sleep_backoff(self) -> None:
        seconds = self._settings.analysis_publish_failure_backoff_seconds
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def _publish_result(self, payload: AnalysisResultMessage, task_id: int) -> None:
        assert self._result_exchange is not None
        body = json.dumps(
            payload.model_dump(by_alias=True, mode="json"), ensure_ascii=False
        ).encode("utf-8")
        await self._result_exchange.publish(
            Message(
                body=body,
                content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                message_id=f"analysis-result-{task_id}",
                correlation_id=str(task_id),
            ),
            routing_key=self._settings.analysis_result_routing_key,
            # 结果队列没绑定时必须报错，而不是静默丢结果
            mandatory=True,
        )
        self._consecutive_publish_failures = 0

    async def _dead_letter(
        self,
        message: AbstractIncomingMessage,
        *,
        error_code: str,
        retries: int = 0,
    ) -> None:
        assert self._dead_exchange is not None
        # 只搬运原始 body 与最小排查信息，不复制 x-death（那是 broker 的结构化状态）。
        # message_id / correlation_id 跟随原消息，死信队列里才能和日志、Spring 侧
        # agent_run 对上号；否则只能靠 body 手工 grep。
        headers: dict[str, Any] = {
            "x-original-queue": self._settings.analysis_request_queue,
            "x-error-code": error_code,
            "x-retry-count": retries,
        }
        if message.message_id:
            headers["x-original-message-id"] = message.message_id
        await self._dead_exchange.publish(
            Message(
                body=message.body,
                content_type=message.content_type or "application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                headers=headers,
            ),
            routing_key=self._settings.analysis_dead_routing_key,
            mandatory=True,
        )
        self._consecutive_publish_failures = 0


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    runtime = AgentRuntime(settings, build_llm_adapter(settings))
    worker = AnalysisWorker(settings, runtime)
    try:
        await worker.start()
    finally:
        await worker.stop()


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        log.info("分析 Worker 已停止")


if __name__ == "__main__":
    main()
