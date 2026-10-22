package com.ails.backend.mq;

import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.AmqpConnectException;
import org.springframework.amqp.AmqpException;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.support.converter.MessageConversionException;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * 分析任务的发布端：投递请求消息，并等待 RabbitMQ 的发布确认。
 * <p>
 * <b>确认不是可选项。</b> 任务行已经写在数据库里（PENDING），如果消息其实没送到 broker，
 * 这个任务会永远停在 PENDING。所以这里必须等到 {@code basic.ack} 才算投递成功；
 * {@code basic.nack}、超时、不可路由（mandatory + return）都抛异常，
 * 由 {@link AnalysisTaskService} 把任务标成 FAILED 并退回同步分析。
 *
 * <h3>为什么还要看 basic.return</h3>
 * 在 {@code mandatory=true} 下，消息无法路由到任何队列时 broker 会先 {@code basic.return}
 * 再 {@code basic.ack}。也就是说「确认成功」并不等于「有人会收到」。这里在确认之后检查
 * {@link CorrelationData#getReturned()}：Spring AMQP 在客户端 I/O 线程处理 return 时
 * 就设置了它，早于同一线程随后完成的 confirm future，因此不存在回调线程的竞态。
 */
@Slf4j
@Component
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
public class AnalysisTaskPublisher {

    private final RabbitTemplate rabbitTemplate;
    private final AnalysisMqProperties properties;

    public AnalysisTaskPublisher(RabbitTemplate rabbitTemplate, AnalysisMqProperties properties) {
        this.rabbitTemplate = rabbitTemplate;
        this.properties = properties;
    }

    /**
     * 投递一条分析任务。方法返回即代表 broker 已确认接收且消息可路由。
     *
     * @throws AnalysisPublishException 连接失败、broker NACK、确认超时或消息不可路由；
     *                                 {@link AnalysisPublishException#outcome()} 区分确定失败与未知结果
     */
    public void publish(AnalysisTaskMessage task) {
        String messageId = String.valueOf(task.taskId());
        CorrelationData correlation = new CorrelationData(messageId);
        try {
            rabbitTemplate.convertAndSend(
                    properties.exchange(),
                    properties.requestRoutingKey(),
                    task,
                    message -> {
                        MessageProperties props = message.getMessageProperties();
                        // 持久化：队列与交换机都是 durable，消息本身也必须持久化，
                        // 否则 broker 重启后任务会消失，而 agent_run 仍停在 PENDING。
                        props.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
                        props.setMessageId(messageId);
                        props.setContentType(MessageProperties.CONTENT_TYPE_JSON);
                        props.setHeader("schemaVersion", task.schemaVersion());
                        return message;
                    },
                    correlation);

            CorrelationData.Confirm confirm = correlation.getFuture()
                    .get(properties.confirmTimeoutMs(), TimeUnit.MILLISECONDS);
            if (confirm == null || !confirm.isAck()) {
                // basic.nack 表示 broker 明确拒收，消息不会进入队列。
                throw AnalysisPublishException.definite("RabbitMQ 未确认接收分析任务："
                        + (confirm == null ? "no confirm" : confirm.getReason()), null);
            }
            if (correlation.getReturned() != null) {
                // mandatory return 表示没有任何队列接收这条消息，同样属于确定失败。
                throw AnalysisPublishException.definite(
                        "分析任务消息不可路由（没有匹配的队列绑定）", null);
            }
            log.debug("分析任务已投递, taskId={}, traceId={}", task.taskId(), task.traceId());
        } catch (InterruptedException e) {
            // 恢复中断标记，让上层线程池能正确感知停机。
            // 消息是否已经写出无法判断，不能当作确定失败。
            Thread.currentThread().interrupt();
            throw AnalysisPublishException.unknown("等待 RabbitMQ 发布确认被中断", e);
        } catch (ExecutionException | TimeoutException e) {
            // 确认 future 超时/通道在等待期间关闭：消息可能已被 broker 接收。
            throw AnalysisPublishException.unknown("等待 RabbitMQ 发布确认失败", e);
        } catch (AmqpConnectException e) {
            // 连接都没建立，消息不可能被 broker 接收。
            throw AnalysisPublishException.definite("连接 RabbitMQ 失败", e);
        } catch (MessageConversionException e) {
            // 序列化失败发生在发送之前。
            throw AnalysisPublishException.definite("分析任务消息序列化失败", e);
        } catch (AmqpException e) {
            // 其他 AMQP 异常可能发生在写 socket 之后（例如通道被关闭）。
            // 保守归为未知，避免因为一次网络抖动触发第二次模型调用。
            throw AnalysisPublishException.unknown("投递分析任务失败（消息可能已写出）", e);
        }
    }
}
