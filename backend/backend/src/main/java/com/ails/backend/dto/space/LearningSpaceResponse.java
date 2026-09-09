package com.ails.backend.dto.space;

import com.ails.backend.entity.LearningSpace;
import com.ails.backend.enums.SpaceVisibility;

import java.time.LocalDateTime;

/**
 * 学习空间响应体。
 */
public record LearningSpaceResponse(
        Long id,
        Long userId,
        String name,
        String description,
        SpaceVisibility visibility,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
    public static LearningSpaceResponse from(LearningSpace space) {
        if (space == null) {
            return null;
        }
        return new LearningSpaceResponse(
                space.getId(), space.getUserId(), space.getName(), space.getDescription(),
                space.getVisibility(), space.getCreatedAt(), space.getUpdatedAt());
    }
}
