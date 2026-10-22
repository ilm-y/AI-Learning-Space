package com.ails.backend.mq;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueBuilder;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;
import org.springframework.amqp.support.converter.MessageConverter;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 学习分析的两条队列与重试/死信拓扑。
 *
 * <pre>
 *  learning.analysis.exchange (direct)
 *      ├─ analysis.request → learning.analysis.request
 *      │       └─ 消费失败且可重试 → DLX → analysis.retry
 *      │              learning.analysis.retry (TTL 15s) → 回到 learning.analysis.request
 *      │       重试耗尽 / 永久错误 → learning.analysis.dlx → learning.analysis.dlq
 *      └─ analysis.result  → learning.analysis.result
 *              └─ 数据库闪断等暂时失败 → analysis.result.retry → TTL → 回到 result 队列
 *              └─ 无法解析 / 重试耗尽 → learning.analysis.dlx → learning.analysis.dlq
 * </pre>
 *
 * <h3>为什么重试用「队列 TTL + DLX」而不是消费者里 sleep</h3>
 * 消费者 sleep 会占住 prefetch 名额；把消息拒绝到 retry 队列后进程立刻空出来，
 * 到期由 broker 重新投递。重试次数由消息上的 {@code x-death} 计数，Worker 判断是否耗尽。
 * <p>
 * 只有 {@code ails.analysis.mode=mq} 时才装配这些 Bean —— inline 模式下没有任何
 * AMQP 连接尝试，这也是新克隆的仓库不装 RabbitMQ 也能启动的原因。
 */
@Configuration
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
@EnableConfigurationProperties({AnalysisMqProperties.class, AnalysisCompensationProperties.class})
@EnableScheduling
public class RabbitMqConfig {

    @Bean
    public DirectExchange analysisExchange(AnalysisMqProperties props) {
        return new DirectExchange(props.exchange(), true, false);
    }

    /** 死信交换机：retry 队列与最终死信队列都挂在这里。 */
    @Bean
    public DirectExchange analysisDeadLetterExchange(AnalysisMqProperties props) {
        return new DirectExchange(props.dlx(), true, false);
    }

    /**
     * 请求队列。被拒绝（requeue=false）的消息进入 retry 队列，而不是立刻重新投递，
     * 避免一个持续失败的任务把消费者打满。
     */
    @Bean
    public Queue analysisRequestQueue(AnalysisMqProperties props) {
        return QueueBuilder.durable(props.requestQueue())
                .withArgument("x-dead-letter-exchange", props.dlx())
                .withArgument("x-dead-letter-routing-key", props.retryRoutingKey())
                .build();
    }

    /** 重试队列：消息在这里停留 retryTtlMs，然后回到请求队列。 */
    @Bean
    public Queue analysisRetryQueue(AnalysisMqProperties props) {
        return QueueBuilder.durable(props.retryQueue())
                .withArgument("x-message-ttl", props.retryTtlMs())
                .withArgument("x-dead-letter-exchange", props.exchange())
                .withArgument("x-dead-letter-routing-key", props.requestRoutingKey())
                .build();
    }

    /**
     * 结果队列。解析失败 / 永久处理失败的消息走 DLQ，不无限 requeue。
     * 注意：结果消息的 {@code default-requeue-rejected} 在 application.yml 里设为 false。
     */
    @Bean
    public Queue analysisResultQueue(AnalysisMqProperties props) {
        return QueueBuilder.durable(props.resultQueue())
                .withArgument("x-dead-letter-exchange", props.dlx())
                .withArgument("x-dead-letter-routing-key", props.deadRoutingKey())
                .build();
    }

    /**
     * 结果处理的重试队列。只在 {@link AnalysisResultConsumer} 捕获到暂时性异常时使用：
     * 复制原消息到这里等待 TTL，再回到结果队列。结果队列本身不能直接 requeue ——
     * 那会让数据库不可用期间的失败消息以最高速度空转。
     */
    @Bean
    public Queue analysisResultRetryQueue(AnalysisMqProperties props) {
        return QueueBuilder.durable(props.resultRetryQueue())
                .withArgument("x-message-ttl", props.retryTtlMs())
                .withArgument("x-dead-letter-exchange", props.exchange())
                .withArgument("x-dead-letter-routing-key", props.resultRoutingKey())
                .build();
    }

    /** 最终死信队列：保留失败消息，供人工排查与补偿。 */
    @Bean
    public Queue analysisDeadQueue(AnalysisMqProperties props) {
        return QueueBuilder.durable(props.deadQueue()).build();
    }

    @Bean
    public Declarables analysisBindings(AnalysisMqProperties props,
                                        DirectExchange analysisExchange,
                                        DirectExchange analysisDeadLetterExchange,
                                        Queue analysisRequestQueue,
                                        Queue analysisRetryQueue,
                                        Queue analysisResultQueue,
                                        Queue analysisResultRetryQueue,
                                        Queue analysisDeadQueue) {
        return new Declarables(
                BindingBuilder.bind(analysisRequestQueue).to(analysisExchange).with(props.requestRoutingKey()),
                BindingBuilder.bind(analysisResultQueue).to(analysisExchange).with(props.resultRoutingKey()),
                BindingBuilder.bind(analysisRetryQueue).to(analysisDeadLetterExchange).with(props.retryRoutingKey()),
                BindingBuilder.bind(analysisResultRetryQueue).to(analysisDeadLetterExchange)
                        .with(props.resultRetryRoutingKey()),
                BindingBuilder.bind(analysisDeadQueue).to(analysisDeadLetterExchange).with(props.deadRoutingKey()));
    }

    /**
     * 消息体统一用 JSON。由 Spring Boot 自动配置的 RabbitTemplate 与监听容器共用。
     * 显式传入应用 ObjectMapper，保证日期等类型的序列化行为与 HTTP 侧一致。
     */
    @Bean
    public MessageConverter analysisJsonMessageConverter(ObjectMapper objectMapper) {
        return new Jackson2JsonMessageConverter(objectMapper);
    }
}
