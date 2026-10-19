package com.ails.backend.dto.memory;

import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;

import java.time.LocalDateTime;

/**
 * Memory 响应体。
 * <p>
 * {@code scopeType} 一并返回，是因为检索可见性完全由它决定：
 * 调用方（前端 / Python Agent）拿到的每一条记忆，都得知道它是「任意空间都可见」
 * 还是「只在这个空间/知识点里可见」，否则没法解释「为什么这条查得到、那条查不到」。
 */
public record MemoryResponse(
        Long id,
        MemoryScopeType scopeType,
        Long learningSpaceId,
        Long conceptId,
        MemoryType memoryType,
        String memoryKey,
        String content,
        MemoryStatus status,
        Float importance,
        LocalDateTime createdAt,
        LocalDateTime updatedAt) {

    public static MemoryResponse from(Memory memory) {
        if (memory == null) {
            return null;
        }
        return new MemoryResponse(
                memory.getId(), memory.getScopeType(), memory.getLearningSpaceId(), memory.getConceptId(),
                memory.getMemoryType(), memory.getMemoryKey(), memory.getContent(), memory.getStatus(),
                memory.getImportance(), memory.getCreatedAt(), memory.getUpdatedAt());
    }
}
