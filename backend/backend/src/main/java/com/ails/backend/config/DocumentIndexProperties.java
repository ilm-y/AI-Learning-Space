package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 文档索引后台任务配置（{@code ails.document.index.*}）。
 *
 * <p>索引任务与聊天 SSE 使用完全独立的线程池：文档解析/向量化可能持续数十秒，
 * 不能占用聊天流式处理线程。线程池必须有界，拒绝时由
 * {@code DocumentIndexService} 把文档置为 FAILED，不能让状态永久停在 PROCESSING。</p>
 *
 * @param corePoolSize   常驻线程数
 * @param maxPoolSize    队列满后允许扩容到的最大线程数
 * @param queueCapacity  有界队列容量
 * @param parseTimeoutMs 调用 Agent 解析接口的总超时
 * @param indexTimeoutMs 调用 Agent 索引接口的总超时
 */
@ConfigurationProperties(prefix = "ails.document.index")
public record DocumentIndexProperties(
        int corePoolSize,
        int maxPoolSize,
        int queueCapacity,
        long parseTimeoutMs,
        long indexTimeoutMs) {

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
    }
}
