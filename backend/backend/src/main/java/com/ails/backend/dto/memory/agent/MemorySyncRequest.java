package com.ails.backend.dto.memory.agent;

/**
 * {@code POST /agent/memories/sync} 请求体。
 *
 * <p>字段名与 Agent 侧的 camelCase 契约逐字对应。{@code operation} 只接受
 * {@code UPSERT} / {@code DELETE}；DELETE 只需要 {@code memoryId} 与 {@code userId}。</p>
 *
 * <p>{@code status} 会写入 Qdrant payload：检索只召回 {@code ACTIVE}，
 * 但保留真实状态便于排查“向量为什么没被检索到”。</p>
 */
public record MemorySyncRequest(
        Long eventId,
        String operation,
        Long memoryId,
        Long userId,
        String content,
        String contentHash,
        Integer memoryVersion,
        String scopeType,
        Long learningSpaceId,
        Long conceptId,
        String memoryType,
        Float importance,
        String status) {
}
