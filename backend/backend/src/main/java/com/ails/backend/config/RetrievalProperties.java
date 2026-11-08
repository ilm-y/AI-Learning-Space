package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 知识检索策略配置（{@code ails.retrieval.*}）。
 *
 * <p><b>RAG-04 阶段 A：默认关闭。</b>关闭时 {@code ready-ids} 的 SQL 与历史行为完全一致：
 * fingerprint 为 NULL 的 legacy 文档仍按 model/provider/dimension 兼容匹配，继续参与检索。
 * 只有产品确认可以接受“legacy 文档暂时不可检索”并完成 RAG-02/03 前置项后，才允许把
 * {@code require-embedding-fingerprint} 翻转为 {@code true}（阶段 B）。</p>
 *
 * <p>开启后进入严格指纹模式：检索白名单必须要求 {@code embedding_fingerprint} 非空且
 * 与调用方当前 fingerprint 精确相等；legacy 文档会被整体排除，不会降级兼容。</p>
 *
 * @param requireEmbeddingFingerprint 是否要求文档携带并精确匹配 embedding fingerprint
 */
@ConfigurationProperties(prefix = "ails.retrieval")
public record RetrievalProperties(Boolean requireEmbeddingFingerprint) {

    public RetrievalProperties {
        if (requireEmbeddingFingerprint == null) {
            requireEmbeddingFingerprint = false;
        }
    }

    /** 严格指纹模式是否开启；空值按关闭处理，任何异常都不应默认收紧检索。 */
    public boolean strictFingerprintEnabled() {
        return Boolean.TRUE.equals(requireEmbeddingFingerprint);
    }
}
