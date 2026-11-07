package com.ails.backend.dto.document.agent;

import java.util.List;

/**
 * {@code POST /agent/documents/delete} 请求体。
 *
 * <p>{@code keepIndexVersion} 为空表示删除该文档的全部版本（文档删除流程）；
 * 非空表示只保留指定活跃版本、清理其余版本（索引切换后的延迟清理）。</p>
 *
 * <p>{@code collection} 优先使用索引时持久化的真实 collection；为空时由 Agent
 * 按当前 provider 配置计算，用于迁移前没有记录 collection 的旧文档。</p>
 *
 * <p>{@code indexVersions} 非空表示只删除这些精确版本；{@code deleteLegacy}
 * 表示同时删除迁移前没有 {@code index_version} 字段的 legacy points。
 * 它与 {@code keepIndexVersion} 互斥。</p>
 */
public record AgentDeleteRequest(
        Long documentId,
        String collection,
        String embeddingModel,
        String embeddingProvider,
        Integer embeddingDimension,
        String embeddingFingerprint,
        Integer keepIndexVersion,
        List<Integer> indexVersions,
        Boolean deleteLegacy) {

    /** 兼容现有调用点：只指定 keepIndexVersion 的旧构造方式。 */
    public AgentDeleteRequest(Long documentId,
                              String collection,
                              String embeddingModel,
                              String embeddingProvider,
                              Integer embeddingDimension,
                              String embeddingFingerprint,
                              Integer keepIndexVersion) {
        this(documentId, collection, embeddingModel, embeddingProvider,
                embeddingDimension, embeddingFingerprint, keepIndexVersion, null, null);
    }
}
