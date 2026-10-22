package com.ails.backend.mq;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * PENDING/RUNNING 任务补偿扫描的参数，对应 {@code ails.analysis.compensation.*}。
 * <p>
 * {@code staleAfterMs} 是「多久没完成就认为异步链路已经放弃」的阈值。它必须明显大于
 * 请求排队时间 + Worker 分析时间 + 结果重试时间之和；默认 60 分钟。
 * 阈值越小，越容易把仍在队列里的任务误判为失败；阈值越大，真正遗留的任务越晚被发现。
 */
@ConfigurationProperties(prefix = "ails.analysis.compensation")
public record AnalysisCompensationProperties(
        boolean enabled,
        long staleAfterMs,
        long intervalMs,
        int batchSize) {

    public AnalysisCompensationProperties {
        if (staleAfterMs <= 0) {
            staleAfterMs = 60 * 60 * 1000L;
        }
        if (intervalMs <= 0) {
            intervalMs = 10 * 60 * 1000L;
        }
        if (batchSize <= 0) {
            batchSize = 100;
        }
    }
}
