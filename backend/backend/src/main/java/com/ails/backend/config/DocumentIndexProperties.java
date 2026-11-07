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
 * @param staleProcessingTimeoutMs PROCESSING/重建租约超过该时长仍无终态，视为索引中断
 * @param recoveryIntervalMs      后台回收扫描间隔
 * @param recoveryInitialDelayMs  启动后首次回收扫描延迟
 * @param recoveryBatchSize       单次扫描最多处理的文档数
 * @param uploadStaleTimeoutMs    UPLOADING 超过该时长仍未提交，视为上传后崩溃
 * @param maxIndexAttempts        UPLOADING 自动重试的最大次数，超过后置 FAILED
 * @param reindexCooldownMs       同一文档两次构建之间的最小间隔，防止狂热提交
 * @param deleteRetryIntervalMs   DELETING 清理重试间隔
 * @param deleteRetryInitialDelayMs DELETING 清理首次扫描延迟
 * @param deleteBatchSize         单次清理扫描的最大文档数
 * @param deleteMaxAttempts       自动清理的最大尝试次数，超过后保持 DELETING 等人工处理
 * @param cleanupIntervalMs       旧索引版本清理扫描间隔
 * @param cleanupInitialDelayMs   旧索引版本清理首次扫描延迟
 * @param cleanupBatchSize        单次版本清理扫描的最大文档数
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
        int recoveryBatchSize,
        long uploadStaleTimeoutMs,
        int maxIndexAttempts,
        long reindexCooldownMs,
        long deleteRetryIntervalMs,
        long deleteRetryInitialDelayMs,
        int deleteBatchSize,
        int deleteMaxAttempts,
        long cleanupIntervalMs,
        long cleanupInitialDelayMs,
        int cleanupBatchSize) {

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
        if (uploadStaleTimeoutMs <= 0) {
            uploadStaleTimeoutMs = 120_000L;
        }
        if (maxIndexAttempts <= 0) {
            maxIndexAttempts = 3;
        }
        if (reindexCooldownMs < 0) {
            reindexCooldownMs = 30_000L;
        }
        if (deleteRetryIntervalMs <= 0) {
            deleteRetryIntervalMs = 60_000L;
        }
        if (deleteRetryInitialDelayMs < 0) {
            deleteRetryInitialDelayMs = 30_000L;
        }
        if (deleteBatchSize <= 0) {
            deleteBatchSize = 50;
        }
        if (deleteMaxAttempts <= 0) {
            deleteMaxAttempts = 20;
        }
        if (cleanupIntervalMs <= 0) {
            cleanupIntervalMs = 300_000L;
        }
        if (cleanupInitialDelayMs < 0) {
            cleanupInitialDelayMs = 120_000L;
        }
        if (cleanupBatchSize <= 0) {
            cleanupBatchSize = 50;
        }
    }
}
