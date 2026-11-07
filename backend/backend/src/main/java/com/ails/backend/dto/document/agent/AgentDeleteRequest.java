package com.ails.backend.dto.document.agent;

/**
 * {@code POST /agent/documents/delete} 请求体。
 *
 * <p>{@code keepIndexVersion} 为空表示删除该文档的全部版本（文档删除流程）；
 * 非空表示只保留指定活跃版本、清理其余版本（索引切换后的延迟清理）。</p>
 *
 * <p>{@code collection} 优先使用索引时持久化的真实 collection；为空时由 Agent
 * 按当前 provider 配置计算，用于迁移前没有记录 collection 的旧文档。</p>
 */
public record AgentDeleteRequest(
        Long documentId,
        String collection,
        String embeddingModel,
        String embeddingProvider,
        Integer embeddingDimension,
        String embeddingFingerprint,
        Integer keepIndexVersion) {
}
