package com.ails.backend.mq;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.amqp.AmqpConnectException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessagePostProcessor;
import org.springframework.amqp.core.ReturnedMessage;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;

/**
 * 发布端的确认语义：只有 broker ack 且消息可路由，publish 才返回成功。
 * 这些分支决定了 AnalysisTaskService 是否会退回同步分析。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisTaskPublisherTest {

    @Mock
    private RabbitTemplate rabbitTemplate;

    private AnalysisTaskPublisher publisher;

    @BeforeEach
    void setUp() {
        publisher = new AnalysisTaskPublisher(rabbitTemplate, properties(200L));
    }

    @Test
    void ackedAndRoutableMessageIsPublished() {
        confirmImmediately(true, null, false);

        publisher.publish(task());
    }

    @Test
    void nackFromBrokerIsDefiniteFailure() {
        confirmImmediately(false, "no space", false);

        assertThatThrownBy(() -> publisher.publish(task()))
                .isInstanceOfSatisfying(AnalysisPublishException.class, e -> {
                    assertThat(e.outcome()).isEqualTo(AnalysisPublishException.Outcome.DEFINITE_FAILURE);
                    assertThat(e.getMessage()).contains("未确认接收");
                });
    }

    @Test
    void confirmTimeoutIsUnknownOutcome() {
        // send 正常返回，但不完成 future：broker 没在超时内确认。
        // 消息可能已经写出，不能当作确定失败，否则会重复调用模型。
        assertThatThrownBy(() -> publisher.publish(task()))
                .isInstanceOfSatisfying(AnalysisPublishException.class, e -> {
                    assertThat(e.outcome()).isEqualTo(AnalysisPublishException.Outcome.UNKNOWN);
                    assertThat(e.getMessage()).contains("发布确认失败");
                });
    }

    @Test
    void unroutableMessageIsDefiniteFailureEvenWhenAcked() {
        // broker 先 basic.return 再 basic.ack：Spring 在 return 帧到达时就把 ReturnedMessage
        // 挂到 CorrelationData 上，确认完成时可见。
        confirmImmediately(true, null, true);

        assertThatThrownBy(() -> publisher.publish(task()))
                .isInstanceOfSatisfying(AnalysisPublishException.class, e -> {
                    assertThat(e.outcome()).isEqualTo(AnalysisPublishException.Outcome.DEFINITE_FAILURE);
                    assertThat(e.getMessage()).contains("不可路由");
                });
    }

    @Test
    void connectionFailureIsDefinite() {
        doThrow(new AmqpConnectException(new IllegalStateException("connection refused")))
                .when(rabbitTemplate).convertAndSend(anyString(), anyString(), any(Object.class),
                        any(MessagePostProcessor.class), any(CorrelationData.class));

        assertThatThrownBy(() -> publisher.publish(task()))
                .isInstanceOfSatisfying(AnalysisPublishException.class, e -> {
                    assertThat(e.outcome()).isEqualTo(AnalysisPublishException.Outcome.DEFINITE_FAILURE);
                    assertThat(e.getMessage()).contains("连接 RabbitMQ 失败");
                });
    }

    @Test
    void otherAmqpFailureIsUnknownBecauseTheMessageMayHaveBeenWritten() {
        doThrow(new org.springframework.amqp.AmqpException("channel closed"))
                .when(rabbitTemplate).convertAndSend(anyString(), anyString(), any(Object.class),
                        any(MessagePostProcessor.class), any(CorrelationData.class));

        // 通道在写出之后关闭时无法判断消息去向；保守归为未知，避免触发第二次模型调用。
        assertThatThrownBy(() -> publisher.publish(task()))
                .isInstanceOfSatisfying(AnalysisPublishException.class, e ->
                        assertThat(e.outcome()).isEqualTo(AnalysisPublishException.Outcome.UNKNOWN));
    }

    /**
     * 模拟 broker 确认。{@code returned=true} 时按真实顺序先挂上 ReturnedMessage 再完成 future，
     * 与 Spring AMQP 在 I/O 线程上的实际行为一致。
     */
    private void confirmImmediately(boolean ack, String reason, boolean returned) {
        doAnswer(invocation -> {
            CorrelationData correlation = invocation.getArgument(4);
            if (returned) {
                correlation.setReturned(new ReturnedMessage(new Message(new byte[0]),
                        312, "NO_ROUTE", "learning.analysis.exchange", "no.route"));
            }
            correlation.getFuture().complete(new CorrelationData.Confirm(ack, reason));
            return null;
        }).when(rabbitTemplate).convertAndSend(anyString(), anyString(), any(Object.class),
                any(MessagePostProcessor.class), any(CorrelationData.class));
    }

    private static AnalysisMqProperties properties(long confirmTimeoutMs) {
        return new AnalysisMqProperties(
                "learning.analysis.exchange", "learning.analysis.dlx",
                "learning.analysis.request", "analysis.request",
                "learning.analysis.result", "analysis.result",
                "learning.analysis.result.retry", "analysis.result.retry",
                "learning.analysis.retry", "analysis.retry",
                "learning.analysis.dlq", "analysis.dead",
                15_000L, 3, confirmTimeoutMs);
    }

    private static AnalysisTaskMessage task() {
        return new AnalysisTaskMessage(
                AnalysisTaskMessage.SCHEMA_VERSION,
                42L, 42L, "trace-1",
                1L, 2L, 3L, null,
                100L, 101L,
                "问题", "回答",
                null, null);
    }
}
