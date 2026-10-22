package com.ails.backend.mq;

import com.ails.backend.mq.AnalysisResultMessage.Status;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.GetResponse;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.core.AcknowledgeMode;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.Declarable;
import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueInformation;
import org.springframework.amqp.rabbit.connection.CachingConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitAdmin;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.rabbit.listener.SimpleMessageListenerContainer;
import org.springframework.amqp.rabbit.listener.api.ChannelAwareMessageListener;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.containers.RabbitMQContainer;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 真实 RabbitMQ Broker 下的链路验证（Testcontainers）。
 * <p>
 * 与 Mock 测试的分工：这里验证的是<b>只有 broker 才能回答的问题</b> ——
 * publisher confirm、mandatory return、TTL + DLX 的路由、未 ACK 消息在 channel 关闭后
 * 重新入队、结果重试副本的确认顺序。数据库事务语义仍由单元测试覆盖。
 * <p>
 * 没有 Docker 时默认跳过（跳过不等于验证通过）；CI 用 {@code -Pmq-integration}
 * 打开 {@code ails.it.require-docker=true}，此时 Docker 缺失会让构建失败，
 * 而不是把「没跑」伪装成「通过」。
 */
class AnalysisMqIntegrationTest {

    private static final String TASK_ID = "42";

    private static RabbitMQContainer rabbit;
    private static AnalysisMqProperties props;
    private static CachingConnectionFactory connectionFactory;
    private static RabbitTemplate template;
    private static RabbitAdmin admin;
    private static Jackson2JsonMessageConverter converter;
    private static AnalysisTaskPublisher publisher;

    @BeforeAll
    static void setUp() {
        if (!DockerClientFactory.instance().isDockerAvailable()) {
            if (Boolean.parseBoolean(System.getProperty("ails.it.require-docker", "false"))) {
                org.junit.jupiter.api.Assertions.fail(
                        "要求真实 RabbitMQ Broker（-Pmq-integration），但本机 Docker 不可用。"
                                + "请启动 Docker 后重跑；直接跳过会把「没验证」伪装成「通过」。");
            }
            Assumptions.abort("Docker 不可用，跳过真实 RabbitMQ 集成测试（跳过 ≠ 通过）");
        }
        rabbit = new RabbitMQContainer("rabbitmq:3.13");
        rabbit.start();

        props = new AnalysisMqProperties(
                "it.exchange", "it.dlx",
                "it.request", "it.request.rk",
                "it.result", "it.result.rk",
                "it.result.retry", "it.result.retry.rk",
                "it.retry", "it.retry.rk",
                "it.dlq", "it.dead.rk",
                300L, 3, 3_000L);

        connectionFactory = connectionFactory(rabbit);
        converter = new Jackson2JsonMessageConverter(new ObjectMapper());
        template = new RabbitTemplate(connectionFactory);
        template.setMessageConverter(converter);

        admin = new RabbitAdmin(connectionFactory);
        admin.afterPropertiesSet();
        declareTopology(admin, props);

        new AnalysisReturnTracker(template);
        publisher = new AnalysisTaskPublisher(template, props);
    }

    @AfterAll
    static void tearDown() {
        if (connectionFactory != null) {
            connectionFactory.destroy();
        }
        if (rabbit != null && rabbit.isRunning()) {
            rabbit.stop();
        }
    }

    /** 每个用例自己清场，避免上一条留下的消息影响下一条的队列计数断言。 */
    @AfterEach
    void purgeQueues() {
        for (String queue : new String[]{
                props.requestQueue(), props.retryQueue(), props.resultQueue(),
                props.resultRetryQueue(), props.deadQueue()}) {
            admin.purgeQueue(queue, true);
        }
    }

    @Test
    void taskMessageIsConfirmedAndPersisted() {
        publisher.publish(task(42L));

        Message received = template.receive(props.requestQueue(), 5_000);
        assertThat(received).isNotNull();
        assertThat(received.getMessageProperties().getMessageId()).isEqualTo(TASK_ID);
        // 从队列里取出的消息，Spring 把投递模式放在 receivedDeliveryMode；getDeliveryMode()
        // 只用于发送端设置的字段，接收端为 null。
        assertThat(received.getMessageProperties().getReceivedDeliveryMode())
                .isEqualTo(MessageDeliveryMode.PERSISTENT);
        assertThat(new String(received.getBody())).contains("\"taskId\":42");
    }

    @Test
    void unroutableTaskIsRejectedEvenThoughBrokerAcks() {
        // 不存在的 routing key：broker 会先 basic.return 再 basic.ack。
        // 发布端必须靠 mandatory return 把「确认成功」纠正成失败。
        AnalysisMqProperties unroutable = new AnalysisMqProperties(
                props.exchange(), props.dlx(),
                props.requestQueue(), "it.no-such-binding",
                props.resultQueue(), props.resultRoutingKey(),
                props.resultRetryQueue(), props.resultRetryRoutingKey(),
                props.retryQueue(), props.retryRoutingKey(),
                props.deadQueue(), props.deadRoutingKey(),
                props.retryTtlMs(), props.maxRetries(), props.confirmTimeoutMs());
        AnalysisTaskPublisher unroutablePublisher =
                new AnalysisTaskPublisher(template, unroutable);

        assertThatThrownBy(() -> unroutablePublisher.publish(task(43L)))
                .isInstanceOfSatisfying(AnalysisPublishException.class, e -> {
                    assertThat(e.outcome()).isEqualTo(AnalysisPublishException.Outcome.DEFINITE_FAILURE);
                    assertThat(e.getMessage()).contains("不可路由");
                });
    }

    @Test
    void unackedRequestReturnsToQueueWhenWorkerCrashesAfterPublishingResult() throws Exception {
        publisher.publish(task(42L));

        // 模拟 Worker：用独立的原生连接取走请求（手动 ACK 模式），发布结果，然后在 ACK 之前崩溃。
        // 不能用 CachingConnectionFactory：它关闭的是缓存 channel，物理 TCP 连接仍在，
        // 未 ACK 消息不会立刻 requeue —— 那是连接池语义，不是 Worker 崩溃语义。
        com.rabbitmq.client.ConnectionFactory rawFactory =
                new com.rabbitmq.client.ConnectionFactory();
        rawFactory.setHost(rabbit.getHost());
        rawFactory.setPort(rabbit.getAmqpPort());
        rawFactory.setUsername(rabbit.getAdminUsername());
        rawFactory.setPassword(rabbit.getAdminPassword());
        com.rabbitmq.client.Connection workerConnection = rawFactory.newConnection();
        try {
            Channel channel = workerConnection.createChannel();
            GetResponse request = channel.basicGet(props.requestQueue(), false);
            assertThat(request).isNotNull();
            template.convertAndSend(props.exchange(), props.resultRoutingKey(), successResult(42L));
            // 不 basicAck，直接关掉整条连接 —— 等价于 Worker 进程在这个点崩溃。
        } finally {
            workerConnection.close();
        }

        awaitMessageCount(props.requestQueue(), 1);
        Message requeued = template.receive(props.requestQueue(), 5_000);
        assertThat(requeued).isNotNull();
        assertThat(requeued.getMessageProperties().getMessageId()).isEqualTo(TASK_ID);
        // 结果已经发出去了，请求又回来了：这正是「至少一次」会重复分析的原因，
        // 由后端 Evidence dedupe / Revision 内容幂等负责收敛。
        awaitMessageCount(props.resultQueue(), 1);
    }

    @Test
    void unroutableResultRetryCopyIsNotAckedEvenThoughBrokerAcks() throws Exception {
        // 结果副本发到不存在的 routing key：broker 仍会 basic.ack，但先发 basic.return。
        // 消费者必须把「副本没路由到任何队列」当成发布失败，nack 原消息而不是 ACK 丢弃。
        AnalysisMqProperties unroutableRetry = new AnalysisMqProperties(
                props.exchange(), props.dlx(),
                props.requestQueue(), props.requestRoutingKey(),
                props.resultQueue(), props.resultRoutingKey(),
                props.resultRetryQueue(), "it.no-such-retry-binding",
                props.retryQueue(), props.retryRoutingKey(),
                props.deadQueue(), props.deadRoutingKey(),
                props.retryTtlMs(), props.maxRetries(), props.confirmTimeoutMs());
        AnalysisResultProcessor processor = mock(AnalysisResultProcessor.class);
        when(processor.process(any(AnalysisResultMessage.class)))
                .thenThrow(new IllegalStateException("db down"));
        AnalysisResultConsumer consumer =
                new AnalysisResultConsumer(processor, template, unroutableRetry);

        Channel channel = mock(Channel.class);
        MessageProperties messageProperties = new MessageProperties();
        messageProperties.setDeliveryTag(7L);
        messageProperties.setContentType(MessageProperties.CONTENT_TYPE_JSON);
        Message original = new Message("{}".getBytes(StandardCharsets.UTF_8), messageProperties);

        consumer.onResult(successResult(42L), original, channel);

        verify(channel).basicNack(7L, false, true);
        verify(channel, never()).basicAck(anyLong(), anyBoolean());
    }

    @Test
    void duplicateResultDeliveryIsConsumedTwiceWithoutLeavingMessages() throws Exception {
        AnalysisResultProcessor processor = mock(AnalysisResultProcessor.class);
        when(processor.process(any(AnalysisResultMessage.class)))
                .thenReturn(AnalysisResultProcessor.Outcome.APPLIED)
                .thenReturn(AnalysisResultProcessor.Outcome.DUPLICATE);
        AnalysisResultConsumer consumer = new AnalysisResultConsumer(processor, template, props);

        SimpleMessageListenerContainer container = startResultConsumer(consumer);
        try {
            template.convertAndSend(props.exchange(), props.resultRoutingKey(), successResult(42L));
            template.convertAndSend(props.exchange(), props.resultRoutingKey(), successResult(42L));

            verify(processor, timeout(10_000).times(2)).process(any(AnalysisResultMessage.class));
            awaitMessageCount(props.resultQueue(), 0);
            awaitMessageCount(props.deadQueue(), 0);
        } finally {
            container.stop();
        }
    }

    @Test
    void consumerCrashAfterCommitIsRetriedFromTtlQueueAndThenDeduplicated() throws Exception {
        AnalysisResultProcessor processor = mock(AnalysisResultProcessor.class);
        when(processor.process(any(AnalysisResultMessage.class)))
                // 第一次：业务已提交，但消费者在 ACK 前崩溃（这里用抛出模拟）
                .thenThrow(new IllegalStateException("simulated crash after commit"))
                // 重投后：按幂等语义返回 DUPLICATE
                .thenReturn(AnalysisResultProcessor.Outcome.DUPLICATE);
        AnalysisResultConsumer consumer = new AnalysisResultConsumer(processor, template, props);

        SimpleMessageListenerContainer container = startResultConsumer(consumer);
        try {
            template.convertAndSend(props.exchange(), props.resultRoutingKey(), successResult(42L));

            verify(processor, timeout(10_000).times(2)).process(any(AnalysisResultMessage.class));
            awaitMessageCount(props.resultQueue(), 0);
            awaitMessageCount(props.resultRetryQueue(), 0);
            awaitMessageCount(props.deadQueue(), 0);
        } finally {
            container.stop();
        }
    }

    @Test
    void retryExhaustionRoutesResultToDeadLetterQueue() throws Exception {
        AnalysisResultProcessor processor = mock(AnalysisResultProcessor.class);
        when(processor.process(any(AnalysisResultMessage.class)))
                .thenThrow(new IllegalStateException("db down"));
        AnalysisResultConsumer consumer = new AnalysisResultConsumer(processor, template, props);

        SimpleMessageListenerContainer container = startResultConsumer(consumer);
        try {
            template.convertAndSend(props.exchange(), props.resultRoutingKey(), successResult(42L));

            verify(processor, timeout(20_000).times(props.maxRetries()))
                    .process(any(AnalysisResultMessage.class));
            verify(processor, timeout(5_000))
                    .markFailedPermanently(eq(42L), anyString());
            awaitMessageCount(props.deadQueue(), 1);
            awaitMessageCount(props.resultQueue(), 0);
        } finally {
            container.stop();
        }
    }

    @Test
    void rejectedRequestTravelsThroughRetryQueueAndReturns() throws Exception {
        template.convertAndSend(props.exchange(), props.requestRoutingKey(), task(42L));

        Channel channel = connectionFactory.createConnection().createChannel(false);
        try {
            GetResponse request = channel.basicGet(props.requestQueue(), false);
            assertThat(request).isNotNull();
            // requeue=false → 请求队列 DLX → retry 队列（TTL）→ 回到请求队列
            channel.basicNack(request.getEnvelope().getDeliveryTag(), false, false);
        } finally {
            channel.close();
        }

        // 消息先在 retry 队列里停留 retryTtlMs，再过期回到请求队列。
        // 只断言「最终回到请求队列」：TTL 很短，中间态可能已经过去。
        awaitMessageCount(props.requestQueue(), 1);
        Message redelivered = template.receive(props.requestQueue(), 5_000);
        assertThat(redelivered).isNotNull();
        assertThat(redelivered.getMessageProperties().getHeaders()).containsKey("x-death");
        assertThat(xDeathQueues(redelivered)).contains(props.retryQueue());
    }

    /**
     * Worker 的「应用重投」路径：副本发布到 DLX 的 retry 路由键，TTL 到期后回到请求队列，
     * 且应用计数头原样保留。这条路径不依赖请求队列的 DLX 转发。
     */
    @Test
    void appRepublishedRetryCopyReturnsToRequestQueueWithCount() throws Exception {
        MessageProperties copyProps = new MessageProperties();
        copyProps.setContentType(MessageProperties.CONTENT_TYPE_JSON);
        copyProps.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
        copyProps.setHeader("x-analysis-retry", 1);
        copyProps.setMessageId("retry-copy-42");
        template.send(props.dlx(), props.retryRoutingKey(),
                new Message("{}".getBytes(StandardCharsets.UTF_8), copyProps));

        // 先停在 retry 队列（TTL 300ms），再回到请求队列
        awaitMessageCount(props.requestQueue(), 1);
        Message back = template.receive(props.requestQueue(), 5_000);
        assertThat(back).isNotNull();
        assertThat(back.getMessageProperties().getHeaders())
                .containsEntry("x-analysis-retry", 1);
        assertThat(back.getMessageProperties().getMessageId()).isEqualTo("retry-copy-42");
    }

    @Test
    void publishFailsAfterBrokerGoesAway() throws Exception {
        RabbitMQContainer broker = new RabbitMQContainer("rabbitmq:3.13");
        broker.start();
        CachingConnectionFactory factory = connectionFactory(broker);
        try {
            RabbitTemplate localTemplate = new RabbitTemplate(factory);
            localTemplate.setMessageConverter(converter);
            RabbitAdmin localAdmin = new RabbitAdmin(factory);
            localAdmin.afterPropertiesSet();
            declareTopology(localAdmin, props);

            // 这个局部 template 需要自己的 mandatory/returns 配置（全局 callback 是模板级）。
            new AnalysisReturnTracker(localTemplate);
            AnalysisTaskPublisher localPublisher =
                    new AnalysisTaskPublisher(localTemplate, props);

            // 先证明链路本来是通的
            localPublisher.publish(task(44L));
            assertThat(localTemplate.receive(props.requestQueue(), 5_000)).isNotNull();

            broker.stop();

            // 连接都没建立：确定失败，Spring 侧可以安全退回同步分析
            assertThatThrownBy(() -> localPublisher.publish(task(45L)))
                    .isInstanceOfSatisfying(AnalysisPublishException.class, e ->
                            assertThat(e.outcome())
                                    .isEqualTo(AnalysisPublishException.Outcome.DEFINITE_FAILURE));
        } finally {
            factory.destroy();
            if (broker.isRunning()) {
                broker.stop();
            }
        }
    }

    // ================= helpers =================

    private static SimpleMessageListenerContainer startResultConsumer(AnalysisResultConsumer consumer) {
        SimpleMessageListenerContainer container = new SimpleMessageListenerContainer(connectionFactory);
        container.setQueueNames(props.resultQueue());
        container.setAcknowledgeMode(AcknowledgeMode.MANUAL);
        container.setPrefetchCount(1);
        container.setDefaultRequeueRejected(false);
        container.setMessageListener((ChannelAwareMessageListener) (message, channel) -> {
            AnalysisResultMessage payload = (AnalysisResultMessage) converter.fromMessage(message);
            consumer.onResult(payload, message, channel);
        });
        container.start();
        return container;
    }

    private static CachingConnectionFactory connectionFactory(RabbitMQContainer broker) {
        CachingConnectionFactory factory =
                new CachingConnectionFactory(broker.getHost(), broker.getAmqpPort());
        factory.setUsername(broker.getAdminUsername());
        factory.setPassword(broker.getAdminPassword());
        factory.setPublisherConfirmType(CachingConnectionFactory.ConfirmType.CORRELATED);
        factory.setPublisherReturns(true);
        return factory;
    }

    private static void declareTopology(RabbitAdmin admin, AnalysisMqProperties p) {
        RabbitMqConfig config = new RabbitMqConfig();
        DirectExchange exchange = config.analysisExchange(p);
        DirectExchange dlx = config.analysisDeadLetterExchange(p);
        Queue request = config.analysisRequestQueue(p);
        Queue retry = config.analysisRetryQueue(p);
        Queue result = config.analysisResultQueue(p);
        Queue resultRetry = config.analysisResultRetryQueue(p);
        Queue dead = config.analysisDeadQueue(p);

        admin.declareExchange(exchange);
        admin.declareExchange(dlx);
        for (Queue queue : new Queue[]{request, retry, result, resultRetry, dead}) {
            admin.declareQueue(queue);
        }
        Declarables bindings = config.analysisBindings(p, exchange, dlx, request, retry, result, resultRetry, dead);
        for (Declarable declarable : bindings.getDeclarables()) {
            if (declarable instanceof Binding binding) {
                admin.declareBinding(binding);
            }
        }
    }

    private static void awaitMessageCount(String queue, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        QueueInformation info = null;
        while (System.nanoTime() < deadline) {
            info = admin.getQueueInfo(queue);
            if (info != null && info.getMessageCount() == expected) {
                return;
            }
            Thread.sleep(50);
        }
        assertThat(info == null ? -1 : info.getMessageCount())
                .as("队列 %s 的消息数", queue)
                .isEqualTo(expected);
    }

    @SuppressWarnings("unchecked")
    private static java.util.List<String> xDeathQueues(Message message) {
        Object deaths = message.getMessageProperties().getHeaders().get("x-death");
        assertThat(deaths).isInstanceOf(java.util.List.class);
        return ((java.util.List<Map<String, Object>>) deaths).stream()
                .map(item -> String.valueOf(item.get("queue")))
                .toList();
    }

    private static AnalysisTaskMessage task(long taskId) {
        return new AnalysisTaskMessage(
                AnalysisTaskMessage.SCHEMA_VERSION,
                taskId, taskId, "trace-" + taskId,
                1L, 2L, 3L, null,
                100L, 101L,
                "什么是闭包？", "闭包是函数和其词法环境的组合。",
                null, null);
    }

    private static AnalysisResultMessage successResult(long taskId) {
        return new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION,
                taskId, "trace-" + taskId, 100L, 101L,
                Status.SUCCEEDED,
                // 结果内容对 Mock 的 processor 不重要；真实反序列化由消息契约测试覆盖。
                null,
                null, null, null);
    }
}
