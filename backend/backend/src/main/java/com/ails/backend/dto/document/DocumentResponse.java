package com.ails.backend.dto.document;

import com.ails.backend.entity.Document;
import com.ails.backend.enums.DocumentStatus;

import java.time.LocalDateTime;

/**
 * 文档元数据响应。
 * <p>
 * 刻意不返回 {@code storageUri}：磁盘/对象存储路径属于服务端内部实现，
 * 客户端只需要通过文档 ID 访问详情。也不返回 {@code deletedAt}。
 * <p>
 * {@code status} 是真实的处理状态。本阶段没有解析/索引能力，新上传文档只会是
 * {@link DocumentStatus#UPLOADING}，不会伪造 {@code READY}。
 */
public record DocumentResponse(
        Long id,
        Long learningSpaceId,
        Long userId,
        String name,
        String fileName,
        String fileType,
        Long size,
        String contentHash,
        DocumentStatus status,
        Integer chunkCount,
        Integer indexAttempts,
        String indexError,
        String embeddingModel,
        Integer indexVersion,
        LocalDateTime indexedAt,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {

    public static DocumentResponse from(Document document) {
        if (document == null) {
            return null;
        }
        return new DocumentResponse(
                document.getId(),
                document.getLearningSpaceId(),
                document.getUserId(),
                document.getName(),
                document.getFileName(),
                document.getFileType(),
                document.getSize(),
                document.getContentHash(),
                document.getStatus(),
                document.getChunkCount(),
                document.getIndexAttempts(),
                document.getIndexError(),
                document.getEmbeddingModel(),
                document.getIndexVersion(),
                document.getIndexedAt(),
                document.getCreatedAt(),
                document.getUpdatedAt());
    }
}
