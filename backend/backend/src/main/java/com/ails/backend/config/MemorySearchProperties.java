package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 混合检索配置（{@code ails.memory.search.*}）。
 *
 * <p>两路并行执行，各自有独立超时；任何一路失败都只降级、不让整个检索失败。</p>
 */
@ConfigurationProperties(prefix = "ails.memory.search")
public record MemorySearchProperties(
        Integer recallK,
        Integer defaultLimit,
        Integer maxLimit,
        Double rrfK,
        Double lexicalWeight,
        Double vectorWeight,
        Long lexicalTimeoutMs,
        Long vectorTimeoutMs,
        Integer executorCoreSize,
        Integer executorMaxSize,
        Integer executorQueueCapacity) {

    public MemorySearchProperties {
        if (recallK == null || recallK <= 0) {
            recallK = 50;
        }
        if (defaultLimit == null || defaultLimit <= 0) {
            defaultLimit = 10;
        }
        if (maxLimit == null || maxLimit <= 0) {
            // 旧接口 /api/memories/search 的 MAX_SEARCH_RESULTS 是 200；混合检索
            // 仍沿用这个安全阀，避免兼容端点因为新实现被静默收窄。
            maxLimit = 200;
        }
        if (maxLimit < defaultLimit) {
            maxLimit = defaultLimit;
        }
        if (rrfK == null || rrfK <= 0) {
            rrfK = 60.0;
        }
        if (lexicalWeight == null || lexicalWeight < 0) {
            lexicalWeight = 1.0;
        }
        if (vectorWeight == null || vectorWeight < 0) {
            vectorWeight = 1.0;
        }
        if (lexicalTimeoutMs == null || lexicalTimeoutMs <= 0) {
            lexicalTimeoutMs = 1_500L;
        }
        if (vectorTimeoutMs == null || vectorTimeoutMs <= 0) {
            vectorTimeoutMs = 2_000L;
        }
        if (executorCoreSize == null || executorCoreSize <= 0) {
            executorCoreSize = 4;
        }
        if (executorMaxSize == null || executorMaxSize < executorCoreSize) {
            executorMaxSize = 8;
        }
        if (executorQueueCapacity == null || executorQueueCapacity < 0) {
            executorQueueCapacity = 64;
        }
    }
}
