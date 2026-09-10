package com.ails.backend.dto.memory;

import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;

import java.time.LocalDateTime;

/**
 * Memory 响应体。
 */
public record MemoryResponse(
        Long id,
        Long learningSpaceId,
        Long conceptId,
        MemoryType memoryType,
        String path,
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
                memory.getId(), memory.getLearningSpaceId(), memory.getConceptId(),
                memory.getMemoryType(), memory.getPath(), memory.getContent(), memory.getStatus(),
                memory.getImportance(), memory.getCreatedAt(), memory.getUpdatedAt());
    }
}
