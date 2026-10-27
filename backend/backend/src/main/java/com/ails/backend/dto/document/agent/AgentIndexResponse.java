package com.ails.backend.dto.document.agent;

import java.util.List;

/**
 * {@code POST /agent/documents/index} 的 data 部分。
 */
public record AgentIndexResponse(
        String collection,
        String embeddingModel,
        String embeddingProvider,
        List<AgentVectorRef> vectors) {
}
