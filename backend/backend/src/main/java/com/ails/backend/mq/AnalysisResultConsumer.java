package com.ails.backend.mq;

import com.rabbitmq.client.Channel;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.AmqpException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * 结果队列的消费者 —— 只做 ACK/NACK，不做业务判断。
 * <p>
 * ACK 时机：结果已经过 {@link AnalysisResultProcessor} 校验与落库。
 * 处理过程中抛异常（数据库闪断等）时先复制到结果重试队列，等待 TTL 后再回到结果队列；
 * 重试次数记在消息头 {@value #RETRY_HEADER} 上。
 * <p>
 * <b>重试耗尽后不是「打日志 + ACK」。</b>消费者会先主动把一份副本发布到死信队列，
 * 等到 broker confirm 且未被 mandatory return 退回，才 ACK 原消息；副本发布失败时
 * 退避后 {@code basicNack(requeue=true)}，把消息还给 broker，绝不静默丢弃。
 * 结果队列自身的 DLX 仍然保留，用于「监听器根本没进来」的消息（反序列化失败等，
 * 由容器的 {@code default-requeue-rejected=false} 拒绝）。
 * <p>
 * 业务失败（结果本身无效）由 processor 返回 FAILED 并正常 ACK，不在这里重试 ——
 * 同一条坏结果重放多少次都不会变好。
 * <p>
 * 为什么常规重试不用 {@code basicNack(requeue=true)}：数据库不可用期间的失败消息会以
 * 最高速度反复投递，既没有退避，也没有次数上限。复制到 TTL 队列让这个重试变成可观测、
 * 可终止的。只有「连重试/死信副本都发不出去」时才短暂退避后 requeue —— 此时 broker
 * 本身可能不可达，requeue 至少不会丢消息。
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
public class AnalysisResultConsumer {

    /** 结果消息已经历的重试次数；与 broker 的 x-death 无关，由本消费者维护。 */
    static final String RETRY_HEADER = "x-result-retry";

    private final AnalysisResultProcessor processor;
    private final RabbitTemplate rabbitTemplate;
    private final AnalysisMqProperties properties;

    @RabbitListener(queues = "${ails.analysis.mq.result-queue}")
    public void onResult(AnalysisResultMessage message, Message amqpMessage, Channel channel)
            throws IOException {
        long deliveryTag = amqpMessage.getMessageProperties().getDeliveryTag();
        Long taskId = message == null ? null : message.taskId();
        try {
            AnalysisResultProcessor.Outcome outcome = processor.process(message);
            // rules-v1 兜底放在主事务提交之后：任务状态已经落定，兜底失败也不会影响它。
            if (outcome == AnalysisResultProcessor.Outcome.FAILED) {
                processor.fallbackIfNeeded(message);
            }
        } catch (Exception e) {
            int attempts = retryAttempts(amqpMessage);
            if (attempts + 1 >= properties.maxRetries()) {
                handleExhausted(taskId, amqpMessage, channel, deliveryTag, attempts, e);
                return;
            }
            try {
                republishForRetry(amqpMessage, attempts + 1);
                // 副本已获得 broker 确认并持久化，原消息才可以确认；否则会同时存在两份。
                channel.basicAck(deliveryTag, false);
            } catch (Exception publishError) {
                // 副本都没发出去：保留原消息，让 broker 重新投递。
                log.error("投递分析结果重试副本失败，原消息重新入队, taskId={}", taskId, publishError);
                backoffThenRequeue(channel, deliveryTag);
            }
            return;
        }
        // ACK 放在业务 try 之外：事务已经提交，此时 ACK 失败只应该让 broker 重投，
        // 重投的消息会被 DUPLICATE 分支挡住；不能把它误判成「处理失败」再复制一份重试消息。
        channel.basicAck(deliveryTag, false);
    }

    /**
     * 重试耗尽：先把一份副本可靠地放进死信队列，再标记任务失败，最后 ACK 原消息。
     * <p>
     * 顺序很重要：<b>死信副本确认在先</b>。如果先标 FAILED 再发死信、而发布失败，消息被
     * requeue 回来时任务已经是终态，processor 只会返回 DUPLICATE，这条结果就真的丢了。
     * 反过来，副本成功而 markFailed 失败时，消息仍在 DLQ 里，run 停在 PENDING/STALE，
     * 补偿扫描与人工排查都能看到它。
     */
    private void handleExhausted(Long taskId, Message original, Channel channel, long deliveryTag,
                                 int attempts, Exception cause) throws IOException {
        try {
            publishCopy(original, properties.dlx(), properties.deadRoutingKey(),
                    "analysis-result-dead", attempts + 1, "RESULT_RETRY_EXHAUSTED", cause);
            log.error("分析结果处理失败且重试耗尽，已转入死信, taskId={}, attempts={}", taskId, attempts, cause);
        } catch (Exception publishError) {
            // 死信副本都发不出去（broker 不可达 / 死信绑定缺失）：绝不 ACK。
            log.error("投递分析结果死信失败，保留原消息等待重投, taskId={}", taskId, publishError);
            backoffThenRequeue(channel, deliveryTag);
            return;
        }
        // 副本已经落盘；这里即使数据库仍然不可用，死信里的消息也足以人工恢复。
        markFailedQuietly(taskId, cause);
        // ACK 失败时 broker 会重投：要么任务已终态返回 DUPLICATE，要么再次进入本分支
        // 再留一份死信副本 —— 两种都不会丢失业务数据。
        channel.basicAck(deliveryTag, false);
    }

    /** 复制一条结果消息到 TTL 重试队列。 */
    private void republishForRetry(Message original, int attempts) {
        publishCopy(original, properties.dlx(), properties.resultRetryRoutingKey(),
                "analysis-result-retry", attempts, null, null);
    }

    /**
     * 复制一条结果消息到指定交换机/路由键，并且<b>等到 broker 确认</b>才返回。
     * <p>
     * 这一步不是「尽力而为」：调用方拿到成功后会 ACK 原消息。如果副本只是写进 socket
     * 而没有落盘，原消息被 ACK 就同时丢掉两份。因此这里必须和任务发布端一样等待
     * publisher confirm，并检查 mandatory return。
     */
    private void publishCopy(Message original, String exchange, String routingKey,
                             String messageIdPrefix, int attempts,
                             String errorCode, Exception cause) {
        MessageProperties source = original.getMessageProperties();
        MessageProperties target = new MessageProperties();
        target.setContentType(source.getContentType() == null
                ? MessageProperties.CONTENT_TYPE_JSON : source.getContentType());
        target.setContentEncoding(source.getContentEncoding());
        target.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
        target.setCorrelationId(source.getCorrelationId());
        target.getHeaders().putAll(source.getHeaders());
        // x-death 由 broker 维护，手工搬运会让重试历史看起来像发生过多次死信。
        target.getHeaders().remove("x-death");
        target.setHeader(RETRY_HEADER, attempts);
        target.setHeader("x-original-message-id", source.getMessageId());
        if (errorCode != null) {
            target.setHeader("x-error-code", errorCode);
        }
        if (cause != null) {
            target.setHeader("x-error-message", truncate(cause.toString(), 500));
        }

        // 每份副本用独立 messageId：Spring 靠它把 basic.return 关联到本次发布的
        // CorrelationData，原消息的 messageId 仍保留在 x-original-message-id 里供排查。
        String copyMessageId = messageIdPrefix + "-" + UUID.randomUUID();
        target.setMessageId(copyMessageId);
        CorrelationData correlation = new CorrelationData(copyMessageId);
        try {
            // retry 队列与死信队列都绑定在 DLX 上，不是主交换机：这里必须发到 DLX。
            rabbitTemplate.send(exchange, routingKey,
                    new Message(original.getBody(), target), correlation);

            CorrelationData.Confirm confirm = correlation.getFuture()
                    .get(properties.confirmTimeoutMs(), TimeUnit.MILLISECONDS);
            if (confirm == null || !confirm.isAck()) {
                throw new IllegalStateException("RabbitMQ 未确认结果副本："
                        + (confirm == null ? "no confirm" : confirm.getReason()));
            }
            // 与任务发布端一致：return 状态由客户端 I/O 线程写入 CorrelationData，
            // 在 confirm future 完成时已经可见；不要依赖 deferred 线程里的全局 callback。
            if (correlation.getReturned() != null) {
                throw new IllegalStateException("结果副本不可路由（" + routingKey + " 绑定缺失）");
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("等待 RabbitMQ 结果副本确认被中断", e);
        } catch (ExecutionException | TimeoutException e) {
            throw new IllegalStateException("等待 RabbitMQ 结果副本确认失败", e);
        } catch (AmqpException e) {
            throw new IllegalStateException("投递结果副本失败", e);
        }
    }

    /**
     * 退避后再把原消息还给 broker。
     * <p>
     * 只有「重试副本/死信副本都发不出去」才会走到这里，通常意味着 broker 或绑定出了
     * 基础设施问题。此时 requeue 是唯一不丢消息的选择；短暂 sleep 是为了避免在 broker
     * 半可用状态下形成无上限的快速重投。sleep 会占住一个 prefetch 名额，属于刻意的代价。
     */
    private void backoffThenRequeue(Channel channel, long deliveryTag) throws IOException {
        long backoffMs = Math.max(0, properties.retryTtlMs());
        if (backoffMs > 0) {
            try {
                Thread.sleep(backoffMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        channel.basicNack(deliveryTag, false, true);
    }

    private void markFailedQuietly(Long taskId, Exception cause) {
        try {
            processor.markFailedPermanently(taskId, "结果消息处理失败且重试耗尽: " + cause);
        } catch (Exception markError) {
            // 数据库可能仍然不可用；至少把消息留在死信里，人工补偿时能看到 taskId。
            log.error("重试耗尽后标记任务失败也失败了, taskId={}", taskId, markError);
        }
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }

    private static int retryAttempts(Message message) {
        Object value = message.getMessageProperties().getHeaders().get(RETRY_HEADER);
        if (value instanceof Number number) {
            return Math.max(0, number.intValue());
        }
        if (value instanceof String text) {
            try {
                return Math.max(0, Integer.parseInt(text));
            } catch (NumberFormatException ignored) {
                return 0;
            }
        }
        return 0;
    }
}
