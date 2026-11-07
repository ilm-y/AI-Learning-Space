package com.ails.backend.dto.memory.agent;

/**
 * {@code POST /agent/memories/search} 请求体。
 *
 * <p>{@code requestId} 只用于日志串联；{@code recallK} 是该路的候选数量，
 * 与最终返回给用户的 limit 无关（融合后再截断）。</p>
 */
public record MemoryVectorSearchRequest(
        String requestId,
        Long userId,
        Long spaceId,
        Long conceptId,
        String query,
        Integer recallK) {
}
