package com.ails.backend.mq;

import com.rabbitmq.client.Channel;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 结果消费者的 ACK/NACK 与有界重试：不连 RabbitMQ，只验证消息层面的决策。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisResultConsumerTest {

    private static final long DELIVERY_TAG = 11L;

    @Mock
    private AnalysisResultProcessor processor;
    @Mock
    private RabbitTemplate rabbitTemplate;
    @Mock
    private Channel channel;

    private AnalysisResultConsumer consumer;
    private AnalysisResultMessage result;

    @BeforeEach
    void setUp() {
        AnalysisMqProperties properties = new AnalysisMqProperties(
                "learning.analysis.exchange", "learning.analysis.dlx",
                "learning.analysis.request", "analysis.request",
                "learning.analysis.result", "analysis.result",
                "learning.analysis.result.retry", "analysis.result.retry",
                "learning.analysis.retry", "analysis.retry",
                "learning.analysis.dlq", "analysis.dead",
                // 退避时长取 0：单测只关心「失败后是否 requeue」，不想真的 sleep 15s。
                0L, 3, 5_000L);
        consumer = new AnalysisResultConsumer(processor, rabbitTemplate, properties);
        result = new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION, 42L, "trace-1", 100L, 101L,
                AnalysisResultMessage.Status.SUCCEEDED,
                null, null, null, null);
    }

    @Test
    void transientFailureIsCopiedToRetryQueueAndAcked() throws IOException {
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        confirmImmediately();

        consumer.onResult(result, messageWithRetries(0), channel);

        ArgumentCaptor<Message> captor = ArgumentCaptor.forClass(Message.class);
        verify(rabbitTemplate).send(eq("learning.analysis.dlx"),
                eq("analysis.result.retry"), captor.capture(), any(CorrelationData.class));
        assertThat(captor.getValue().getMessageProperties().getHeaders())
                .containsEntry(AnalysisResultConsumer.RETRY_HEADER, 1);
        verify(channel).basicAck(DELIVERY_TAG, false);
        verify(channel, never()).basicNack(DELIVERY_TAG, false, false);
    }

    @Test
    void unconfirmedRetryCopyLeavesOriginalUnacked() throws IOException {
        consumer = consumerWithConfirmTimeout(200L);
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        // 不完成 confirm future：超时按「broker 未收下」处理。

        consumer.onResult(result, messageWithRetries(0), channel);

        verify(channel).basicNack(DELIVERY_TAG, false, true);
        verify(channel, never()).basicAck(DELIVERY_TAG, false);
    }

    @Test
    void returnedRetryCopyLeavesOriginalUnacked() throws IOException {
        consumer = consumerWithConfirmTimeout(200L);
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        confirmImmediately(true);

        consumer.onResult(result, messageWithRetries(0), channel);

        verify(channel).basicNack(DELIVERY_TAG, false, true);
        verify(channel, never()).basicAck(DELIVERY_TAG, false);
    }

    @Test
    void exhaustedResultIsCopiedToDeadLetterBeforeAck() throws IOException {
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        confirmImmediately();

        consumer.onResult(result, messageWithRetries(2), channel);

        ArgumentCaptor<Message> captor = ArgumentCaptor.forClass(Message.class);
        verify(rabbitTemplate).send(eq("learning.analysis.dlx"), eq("analysis.dead"),
                captor.capture(), any(CorrelationData.class));
        assertThat(captor.getValue().getMessageProperties().getHeaders())
                .containsEntry("x-error-code", "RESULT_RETRY_EXHAUSTED")
                .containsEntry(AnalysisResultConsumer.RETRY_HEADER, 3);
        verify(processor).markFailedPermanently(eq(42L), anyString());
        verify(channel).basicAck(DELIVERY_TAG, false);
        verify(channel, never()).basicNack(anyLong(), anyBoolean(), anyBoolean());
    }

    @Test
    void deadLetterPublishFailureKeepsOriginalUnackedAndDoesNotMarkFailed() throws IOException {
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        doThrow(new IllegalStateException("broker down"))
                .when(rabbitTemplate).send(anyString(), anyString(), any(Message.class),
                        any(CorrelationData.class));

        consumer.onResult(result, messageWithRetries(2), channel);

        // 死信副本没落地：消息必须还给 broker，不能 ACK 丢弃
        verify(channel).basicNack(DELIVERY_TAG, false, true);
        verify(channel, never()).basicAck(DELIVERY_TAG, false);
        // 顺序：先确认死信副本，再改任务状态 —— 否则重投回来时任务已终态，结果就真丢了
        verify(processor, never()).markFailedPermanently(eq(42L), anyString());
    }

    @Test
    void unconfirmedDeadLetterCopyLeavesOriginalUnacked() throws IOException {
        consumer = consumerWithConfirmTimeout(200L);
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        // 不完成 confirm future：超时按「broker 未收下」处理。

        consumer.onResult(result, messageWithRetries(2), channel);

        verify(channel).basicNack(DELIVERY_TAG, false, true);
        verify(channel, never()).basicAck(DELIVERY_TAG, false);
        verify(processor, never()).markFailedPermanently(eq(42L), anyString());
    }

    @Test
    void successIsAckedWithoutPublishingAnything() throws IOException {
        when(processor.process(result)).thenReturn(AnalysisResultProcessor.Outcome.APPLIED);

        consumer.onResult(result, messageWithRetries(0), channel);

        verify(channel).basicAck(DELIVERY_TAG, false);
        verify(rabbitTemplate, never()).send(anyString(), anyString(), any(Message.class),
                any(CorrelationData.class));
    }

    @Test
    void ackFailureAfterCommitDoesNotPublishRetryCopy() throws IOException {
        when(processor.process(result)).thenReturn(AnalysisResultProcessor.Outcome.APPLIED);
        doThrow(new IOException("channel closed")).when(channel).basicAck(DELIVERY_TAG, false);

        assertThatThrownBy(() -> consumer.onResult(result, messageWithRetries(0), channel))
                .isInstanceOf(IOException.class);

        // 事务已经提交；ACK 失败只应该让 broker 重投，重投会被 DUPLICATE 挡住。
        // 绝不能因此复制一条重试消息 —— 那会让同一份结果同时在两个队列里。
        verify(rabbitTemplate, never()).send(anyString(), anyString(), any(Message.class),
                any(CorrelationData.class));
        verify(channel, never()).basicNack(anyLong(), anyBoolean(), anyBoolean());
    }

    @Test
    void failureToPublishRetryCopyLeavesOriginalUnacked() throws IOException {
        when(processor.process(result)).thenThrow(new IllegalStateException("db down"));
        doThrow(new IllegalStateException("broker down"))
                .when(rabbitTemplate).send(anyString(), anyString(), any(Message.class),
                        any(CorrelationData.class));

        consumer.onResult(result, messageWithRetries(0), channel);

        verify(channel).basicNack(DELIVERY_TAG, false, true);
        verify(channel, never()).basicAck(DELIVERY_TAG, false);
    }

    private AnalysisResultConsumer consumerWithConfirmTimeout(long confirmTimeoutMs) {
        AnalysisMqProperties properties = new AnalysisMqProperties(
                "learning.analysis.exchange", "learning.analysis.dlx",
                "learning.analysis.request", "analysis.request",
                "learning.analysis.result", "analysis.result",
                "learning.analysis.result.retry", "analysis.result.retry",
                "learning.analysis.retry", "analysis.retry",
                "learning.analysis.dlq", "analysis.dead",
                0L, 3, confirmTimeoutMs);
        return new AnalysisResultConsumer(processor, rabbitTemplate, properties);
    }

    /** 模拟 broker 确认：send 一返回就完成 CorrelationData 的 future。 */
    private void confirmImmediately() {
        confirmImmediately(false);
    }

    /**
     * 模拟 broker 确认；{@code returned=true} 时按真实顺序先设置 ReturnedMessage 再完成 future。
     */
    private void confirmImmediately(boolean returned) {
        doAnswer(invocation -> {
            CorrelationData correlation = invocation.getArgument(3);
            if (returned) {
                correlation.setReturned(new org.springframework.amqp.core.ReturnedMessage(
                        new Message(new byte[0]), 312, "NO_ROUTE",
                        "learning.analysis.dlx", "analysis.result.retry"));
            }
            correlation.getFuture().complete(new CorrelationData.Confirm(true, null));
            return null;
        }).when(rabbitTemplate).send(anyString(), anyString(), any(Message.class),
                any(CorrelationData.class));
    }

    private static Message messageWithRetries(int retries) {
        MessageProperties properties = new MessageProperties();
        properties.setDeliveryTag(DELIVERY_TAG);
        properties.setContentType(MessageProperties.CONTENT_TYPE_JSON);
        if (retries > 0) {
            properties.setHeader(AnalysisResultConsumer.RETRY_HEADER, retries);
        }
        return new Message("{}".getBytes(StandardCharsets.UTF_8), properties);
    }
}
