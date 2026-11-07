package com.ails.backend.dto.memory.agent;

/**
 * 向量路的一条候选。
 *
 * <p><b>不包含正文</b>：正文必须由 Spring 回 MySQL 取，避免 Qdrant 中的陈旧内容
 * 直接进入用户可见结果。</p>
 */
public record MemoryVectorHit(
        Long memoryId,
        double score,
        String contentHash) {
}
