package com.ails.backend.mq;

import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * 在 {@link RabbitTemplate} 上安装 mandatory return 的全局 callback，用于记录不可路由的消息。
 * <p>
 * {@code basic.return} 只说明消息没有路由到任何队列，broker 随后仍会发送 {@code basic.ack}
 * —— 也就是说 <b>confirm 成功并不等于有人会收到</b>。
 * <p>
 * <b>不要用这个 callback 的副作用判断「是否被退回」。</b>Spring AMQP 把 return callback
 * 放到 deferred 线程池执行，confirm future 可能先完成；在发布线程里读 callback 写入的标记
 * 存在竞态。发布方判断不可路由必须读 {@link org.springframework.amqp.rabbit.connection.CorrelationData#getReturned()}：
 * 它在 RabbitMQ 客户端 I/O 线程处理 {@code basic.return} 时就已设置，且 broker 保证
 * return 帧先于随后的 ack 帧，所以 confirm future 完成时该值已经可靠可见。
 * <p>
 * callback 只能有一个，因此任务发布与结果重试/死信副本共用这一份；这里只做日志记录。
 */
@Slf4j
@Component
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
public class AnalysisReturnTracker {

    public AnalysisReturnTracker(RabbitTemplate rabbitTemplate) {
        // mandatory 让「没有队列可路由」变成一条 basic.return，而不是静默丢弃。
        rabbitTemplate.setMandatory(true);
        rabbitTemplate.setReturnsCallback(returnedMessage -> log.error(
                "分析消息不可路由：exchange={}, routingKey={}, replyCode={}, replyText={}, messageId={}",
                returnedMessage.getExchange(), returnedMessage.getRoutingKey(),
                returnedMessage.getReplyCode(), returnedMessage.getReplyText(),
                returnedMessage.getMessage().getMessageProperties().getMessageId()));
    }
}
