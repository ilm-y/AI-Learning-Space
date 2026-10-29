package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 文档索引后台任务配置（{@code ails.document.index.*}）。
 *
 * <p>索引任务与聊天 SSE 使用完全独立的线程池：文档解析/向量化可能持续数十秒，
 * 不能占用聊天流式处理线程。线程池必须有界，拒绝时由
 * {@code DocumentIndexService} 把文档置为 FAILED，不能让状态永久停在 PROCESSING。</p>
 *
 * @param corePoolSize            常驻线程数
 * @param maxPoolSize             队列满后允许扩容到的最大线程数
 * @param queueCapacity           有界队列容量
 * @param parseTimeoutMs          调用 Agent 解析接口的总超时
 * @param indexTimeoutMs          调用 Agent 索引接口的总超时
 * @param staleProcessingTimeoutMs PROCESSING 超过该时长仍无终态，视为索引中断
 * @param recoveryIntervalMs      后台回收扫描间隔
 * @param recoveryInitialDelayMs  启动后首次回收扫描延迟
 * @param recoveryBatchSize       单次扫描最多处理的文档数
 */
@ConfigurationProperties(prefix = "ails.document.index")
public record DocumentIndexProperties(
        int corePoolSize,
        int maxPoolSize,
        int queueCapacity,
        long parseTimeoutMs,
        long indexTimeoutMs,
        long staleProcessingTimeoutMs,
        long recoveryIntervalMs,
        long recoveryInitialDelayMs,
        int recoveryBatchSize) {

    public DocumentIndexProperties {
        if (corePoolSize <= 0) {
            corePoolSize = 2;
        }
        if (maxPoolSize < corePoolSize) {
            maxPoolSize = corePoolSize;
        }
        if (queueCapacity < 0) {
            queueCapacity = 64;
        }
        if (parseTimeoutMs <= 0) {
            parseTimeoutMs = 60_000L;
        }
        if (indexTimeoutMs <= 0) {
            indexTimeoutMs = 120_000L;
        }
        // 默认值必须明显大于一次正常索引的最坏耗时（parse + index 两个 HTTP 超时，
        // 再加 Embedding / Qdrant 与 MySQL 写入）。15 分钟不是「短超时」：
        // 正常任务会在分钟级完成，只有进程中断/挂死才会留到这么久。
        if (staleProcessingTimeoutMs <= 0) {
            staleProcessingTimeoutMs = 900_000L;
        }
        if (recoveryIntervalMs <= 0) {
            recoveryIntervalMs = 300_000L;
        }
        if (recoveryInitialDelayMs < 0) {
            recoveryInitialDelayMs = 60_000L;
        }
        if (recoveryBatchSize <= 0) {
            recoveryBatchSize = 100;
        }
    }
}
