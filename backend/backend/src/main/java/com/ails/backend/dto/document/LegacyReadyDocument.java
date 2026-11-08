package com.ails.backend.dto.document;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 一条 legacy READY 文档的诊断记录（{@code embedding_fingerprint IS NULL}）。
 *
 * <p>只用于运维排查和 RAG-04 阶段 B 的重建清单；<b>不参与 Agent 的正常检索白名单</b>。
 * {@code vectorCollection} 可能为 NULL（迁移前未记录实际写入的 collection），如实返回，
 * 不猜测、不补默认值。</p>
 */
@Data
public class LegacyReadyDocument {

    private Long id;

    private Long userId;

    private Long learningSpaceId;

    private String embeddingModel;

    private String vectorCollection;

    private LocalDateTime indexedAt;
}
