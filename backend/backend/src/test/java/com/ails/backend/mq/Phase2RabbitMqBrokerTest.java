package com.ails.backend.mq;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.ConnectionFactory;
import com.rabbitmq.client.GetResponse;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.Declarable;
import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueInformation;
import org.springframework.amqp.rabbit.connection.CachingConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitAdmin;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;

import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Phase 2 C：真实 RabbitMQ Broker 上的发布 / 消费 / 重复投递 / 重试 / 死信。
 *
 * <pre>
 * AILS_PHASE2_MQ_IT=true ./mvnw -Dtest=Phase2RabbitMqBrokerTest test
 * </pre>
 *
 * <p>与 {@link AnalysisMqIntegrationTest} 的分工：后者用 Testcontainers 起独立 broker，
 * 本机 Testcontainers 无法访问 Docker 时会被跳过；本类直接连接已运行的本地 Broker
 * （默认 {@code 127.0.0.1:5672}），因此在本环境能真正执行。两者都不 Mock broker。</p>
 *
 * <p>业务落库的最终状态由 {@link Phase2MqProcessorIntegrationTest}（真实 MySQL）断言；
 * 本类只回答「消息有没有真正经过 broker、重复投递是否可识别、失败是否进入重试/死信」。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_PHASE2_MQ_IT", matches = "true")
class Phase2RabbitMqBrokerTest {

    private static final String SUFFIX = "phase2" + (System.currentTimeMillis() % 100000);

    private static AnalysisMqProperties props;
    private static CachingConnectionFactory factory;
    private static RabbitTemplate template;
    private static RabbitAdmin admin;
    private static AnalysisTaskPublisher publisher;

    @BeforeAll
    static void setUp() throws Exception {
        props = new AnalysisMqProperties(
                SUFFIX + ".exchange", SUFFIX + ".dlx",
                SUFFIX + ".request", SUFFIX + ".request.rk",
                SUFFIX + ".result", SUFFIX + ".result.rk",
                SUFFIX + ".result.retry", SUFFIX + ".result.retry.rk",
                SUFFIX + ".retry", SUFFIX + ".retry.rk",
                SUFFIX + ".dlq", SUFFIX + ".dead.rk",
                400L, 2, 5_000L);

        factory = new CachingConnectionFactory(env("AILS_PHASE2_MQ_HOST", "127.0.0.1"),
                Integer.parseInt(env("AILS_PHASE2_MQ_PORT", "5672")));
        factory.setUsername(env("AILS_PHASE2_MQ_USER", "guest"));
        factory.setPassword(env("AILS_PHASE2_MQ_PASSWORD", ""));
        factory.setPublisherConfirmType(CachingConnectionFactory.ConfirmType.CORRELATED);
        factory.setPublisherReturns(true);

        template = new RabbitTemplate(factory);
        template.setMessageConverter(new Jackson2JsonMessageConverter(new ObjectMapper()));
        template.setMandatory(true);
        new AnalysisReturnTracker(template);

        admin = new RabbitAdmin(factory);
        admin.afterPropertiesSet();
        declareTopology(admin, props);
        publisher = new AnalysisTaskPublisher(template, props);
    }

    @AfterAll
    static void tearDown() {
        if (admin != null) {
            for (String queue : queues()) {
                admin.deleteQueue(queue);
            }
            admin.deleteExchange(props.exchange());
            admin.deleteExchange(props.dlx());
        }
        if (factory != null) {
            factory.destroy();
        }
    }

    @AfterEach
    void purgeQueues() {
        for (String queue : queues()) {
            admin.purgeQueue(queue, true);
        }
    }

    @Test
    @DisplayName("C5 发布确认后消息真实进入请求队列，可被消费")
    void publishIsConfirmedAndConsumed() {
        publisher.publish(task(42L));

        org.springframework.amqp.core.Message received = template.receive(props.requestQueue(), 5_000);

        assertThat(received).isNotNull();
        assertThat(received.getMessageProperties().getMessageId()).isEqualTo("42");
        assertThat(new String(received.getBody())).contains("\"taskId\":42");
    }

    @Test
    @DisplayName("C6 同一任务重复投递会产生两条消息，消费端需靠 taskId/DB 幂等收敛")
    void duplicatePublishIsVisibleToBroker() throws Exception {
        publisher.publish(task(43L));
        publisher.publish(task(43L));

        awaitMessageCount(props.requestQueue(), 2);
        var first = template.receive(props.requestQueue(), 5_000);
        var second = template.receive(props.requestQueue(), 5_000);
        assertThat(first).isNotNull();
        assertThat(second).isNotNull();
        assertThat(first.getMessageProperties().getMessageId()).isEqualTo("43");
        assertThat(second.getMessageProperties().getMessageId()).isEqualTo("43");
    }

    @Test
    @DisplayName("C7 未 ACK 的请求在消费者连接断开后重新入队（至少一次投递）")
    void unackedMessageIsRedelivered() throws Exception {
        publisher.publish(task(44L));

        ConnectionFactory rawFactory = new ConnectionFactory();
        rawFactory.setHost(env("AILS_PHASE2_MQ_HOST", "127.0.0.1"));
        rawFactory.setPort(Integer.parseInt(env("AILS_PHASE2_MQ_PORT", "5672")));
        rawFactory.setUsername(env("AILS_PHASE2_MQ_USER", "guest"));
        rawFactory.setPassword(env("AILS_PHASE2_MQ_PASSWORD", ""));
        com.rabbitmq.client.Connection worker = rawFactory.newConnection();
        try {
            Channel channel = worker.createChannel();
            GetResponse taken = channel.basicGet(props.requestQueue(), false);
            assertThat(taken).isNotNull();
            // 不 ACK，直接断开整条连接 = Worker 崩溃
        } finally {
            worker.close();
        }

        awaitMessageCount(props.requestQueue(), 1);
        var redelivered = template.receive(props.requestQueue(), 5_000);
        assertThat(redelivered).isNotNull();
        assertThat(redelivered.getMessageProperties().getMessageId()).isEqualTo("44");
    }

    @Test
    @DisplayName("C8 请求失败进入重试队列，TTL 到期后回到请求队列")
    void retryQueueReturnsMessageAfterTtl() throws Exception {
        // 直接投到 DLX + retry routing key，等价于消费者 basicNack(requeue=false)
        template.convertAndSend(props.dlx(), props.retryRoutingKey(), task(45L));

        awaitMessageCount(props.retryQueue(), 1);
        // TTL 400ms 到期后由 broker 死信回请求队列
        awaitMessageCount(props.requestQueue(), 1);
        var retried = template.receive(props.requestQueue(), 5_000);
        assertThat(retried).isNotNull();
        assertThat(new String(retried.getBody())).contains("\"taskId\":45");
    }

    @Test
    @DisplayName("C9 结果消息被拒绝后进入最终死信队列，不会无限重投")
    void rejectedResultReachesDeadQueue() throws Exception {
        template.convertAndSend(props.exchange(), props.resultRoutingKey(), task(46L));
        awaitMessageCount(props.resultQueue(), 1);

        ConnectionFactory rawFactory = new ConnectionFactory();
        rawFactory.setHost(env("AILS_PHASE2_MQ_HOST", "127.0.0.1"));
        rawFactory.setPort(Integer.parseInt(env("AILS_PHASE2_MQ_PORT", "5672")));
        rawFactory.setUsername(env("AILS_PHASE2_MQ_USER", "guest"));
        rawFactory.setPassword(env("AILS_PHASE2_MQ_PASSWORD", ""));
        try (com.rabbitmq.client.Connection connection = rawFactory.newConnection();
             Channel channel = connection.createChannel()) {
            GetResponse result = channel.basicGet(props.resultQueue(), false);
            assertThat(result).isNotNull();
            channel.basicNack(result.getEnvelope().getDeliveryTag(), false, false);
        }

        awaitMessageCount(props.deadQueue(), 1);
        var dead = template.receive(props.deadQueue(), 5_000);
        assertThat(dead).isNotNull();
        assertThat(new String(dead.getBody())).contains("\"taskId\":46");
    }

    // ===================== helpers =====================

    private static String[] queues() {
        return new String[]{props.requestQueue(), props.retryQueue(), props.resultQueue(),
                props.resultRetryQueue(), props.deadQueue()};
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

    private static AnalysisTaskMessage task(long taskId) {
        return new AnalysisTaskMessage(
                AnalysisTaskMessage.SCHEMA_VERSION, taskId, taskId, "trace-" + taskId,
                1L, 2L, 3L, null, 100L, 101L,
                "什么是闭包？", "闭包是函数和其词法环境的组合。",
                null, null);
    }

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }
}
