package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Qdrant → MySQL 反向对账配置（{@code ails.document.reconcile.*}）。
 *
 * <p>对账是轻量 COUNT 查询，不读取向量、不修改文档 status；默认每小时一次，
 * 单批最多 {@code batchSize} 个文档，避免多租户下一次性打满 Agent/Qdrant。</p>
 *
 * @param enabled        是否启用（默认 true）
 * @param intervalMs     两次对账之间的固定延迟
 * @param initialDelayMs 启动后首次对账延迟
 * @param batchSize      单次扫描文档数上限
 * @param leaseTtlMs     多实例互斥租约有效期；应明显大于单批最坏耗时
 */
@ConfigurationProperties(prefix = "ails.document.reconcile")
public record DocumentReconciliationProperties(
        boolean enabled,
        long intervalMs,
        long initialDelayMs,
        int batchSize,
        long leaseTtlMs) {

    public DocumentReconciliationProperties {
        if (intervalMs <= 0) {
            intervalMs = 3_600_000L;
        }
        if (initialDelayMs < 0) {
            initialDelayMs = 300_000L;
        }
        if (batchSize <= 0) {
            batchSize = 200;
        }
        if (leaseTtlMs <= 0) {
            leaseTtlMs = 1_800_000L;
        }
    }
}
