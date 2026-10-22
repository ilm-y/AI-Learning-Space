package com.ails.backend.mq;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 学习分析队列的拓扑与可靠性参数，对应 {@code ails.analysis.mq.*}。
 * <p>
 * 用 record 做构造器绑定：字段即契约，新增拓扑时必须一起改这里和 application.yml。
 */
@ConfigurationProperties(prefix = "ails.analysis.mq")
public record AnalysisMqProperties(
        String exchange,
        String dlx,
        String requestQueue,
        String requestRoutingKey,
        String resultQueue,
        String resultRoutingKey,
        /** 结果处理遇到数据库闪断等暂时性失败时的延迟重试队列。 */
        String resultRetryQueue,
        String resultRetryRoutingKey,
        String retryQueue,
        String retryRoutingKey,
        String deadQueue,
        String deadRoutingKey,
        long retryTtlMs,
        int maxRetries,
        long confirmTimeoutMs) {
}
