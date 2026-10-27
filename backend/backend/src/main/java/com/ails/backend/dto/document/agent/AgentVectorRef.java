package com.ails.backend.dto.document.agent;

/**
 * MySQL chunk ID → Qdrant point ID 的映射。
 */
public record AgentVectorRef(
        Long chunkId,
        String pointId) {
}
