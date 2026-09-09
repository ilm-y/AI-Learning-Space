package com.ails.backend.dto.state;

import com.ails.backend.enums.LearningStateStatus;

import java.time.LocalDateTime;

/**
 * 学习状态响应体。
 */
public record LearningStateResponse(
        Long userId,
        Long conceptId,
        String conceptName,
        Double mastery,
        Double confidence,
        Double stability,
        LearningStateStatus status,
        LocalDateTime lastEvidenceAt,
        LocalDateTime nextReviewAt,
        Integer reviewPriority,
        LocalDateTime updatedAt
) {
}
