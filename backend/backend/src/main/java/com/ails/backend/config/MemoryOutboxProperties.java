package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Memory Outbox Relay 配置（{@code ails.memory.outbox.*}）。
 *
 * <p>默认值遵循设计文档 §4.3：2s 轮询、每批 100、租约 120s、最多 8 次、
 * 指数退避 2s → 5min、±20% 抖动。</p>
 */
@ConfigurationProperties(prefix = "ails.memory.outbox")
public record MemoryOutboxProperties(
        Boolean enabled,
        Long intervalMs,
        Long initialDelayMs,
        Integer batchSize,
        Integer maxBatchPerTick,
        Long leaseSeconds,
        Integer maxAttempts,
        Long baseDelayMs,
        Long maxDelayMs,
        Double jitterRatio,
        Long cleanupIntervalMs,
        Long cleanupInitialDelayMs,
        Integer cleanupBatchSize) {

    public MemoryOutboxProperties {
        if (enabled == null) {
            enabled = Boolean.TRUE;
        }
        if (intervalMs == null || intervalMs <= 0) {
            intervalMs = 2_000L;
        }
        if (initialDelayMs == null || initialDelayMs < 0) {
            initialDelayMs = 10_000L;
        }
        if (batchSize == null || batchSize <= 0) {
            batchSize = 100;
        }
        if (maxBatchPerTick == null || maxBatchPerTick <= 0) {
            maxBatchPerTick = 1_000;
        }
        if (leaseSeconds == null || leaseSeconds <= 0) {
            leaseSeconds = 120L;
        }
        if (maxAttempts == null || maxAttempts <= 0) {
            maxAttempts = 8;
        }
        if (baseDelayMs == null || baseDelayMs <= 0) {
            baseDelayMs = 2_000L;
        }
        if (maxDelayMs == null || maxDelayMs < baseDelayMs) {
            maxDelayMs = 300_000L;
        }
        if (jitterRatio == null || jitterRatio < 0) {
            jitterRatio = 0.2;
        }
        if (cleanupIntervalMs == null || cleanupIntervalMs <= 0) {
            cleanupIntervalMs = 86_400_000L;
        }
        if (cleanupInitialDelayMs == null || cleanupInitialDelayMs < 0) {
            cleanupInitialDelayMs = 60_000L;
        }
        if (cleanupBatchSize == null || cleanupBatchSize <= 0) {
            cleanupBatchSize = 1_000;
        }
    }

    public boolean relayEnabled() {
        return Boolean.TRUE.equals(enabled);
    }
}
