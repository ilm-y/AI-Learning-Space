package com.ails.backend.dto.memory.agent;

import com.ails.backend.entity.Memory;
import com.ails.backend.utils.Hashing;

/**
 * Outbox 事件的自包含快照。
 *
 * <p>字段与设计文档 §4.2 一致。{@code contentHash} 固定为
 * <b>UTF-8 原始 content 的 SHA-256</b>（不做 normalize），与迁移脚本的
 * {@code SHA2(content, 256)} 及 Agent 侧的 {@code hashlib.sha256} 必须得到同一串，
 * 否则「向量索引是否落后」无法比对。</p>
 */
public record MemoryEventPayload(
        Long memoryId,
        Long userId,
        String content,
        String contentHash,
        Integer memoryVersion,
        String scopeType,
        String scopeKey,
        Long learningSpaceId,
        Long conceptId,
        String memoryType,
        String status,
        Float importance,
        String updatedAt) {

    public static MemoryEventPayload from(Memory memory) {
        return new MemoryEventPayload(
                memory.getId(),
                memory.getUserId(),
                memory.getContent(),
                memory.getContent() == null ? null : Hashing.sha256Hex(memory.getContent()),
                memory.getVersion(),
                memory.getScopeType() == null ? null : memory.getScopeType().name(),
                memory.getScopeKey(),
                memory.getLearningSpaceId(),
                memory.getConceptId(),
                memory.getMemoryType() == null ? null : memory.getMemoryType().name(),
                memory.getStatus() == null ? null : memory.getStatus().name(),
                memory.getImportance(),
                memory.getUpdatedAt() == null ? null : memory.getUpdatedAt().toString());
    }
}
