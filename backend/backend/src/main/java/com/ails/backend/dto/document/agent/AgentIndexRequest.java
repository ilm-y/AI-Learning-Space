package com.ails.backend.dto.document.agent;

import java.util.List;

/**
 * {@code POST /agent/documents/index} 请求体。
 *
 * <p>Embedding Provider、模型和 collection 由 Agent 配置决定，不在请求里由
 * Spring 指定，避免两端各自选择不同模型而混用不兼容向量。</p>
 */
public record AgentIndexRequest(
        Long documentId,
        Long userId,
        Long spaceId,
        String documentName,
        String fileName,
        String fileType,
        Integer indexVersion,
        List<AgentIndexChunk> chunks) {
}
