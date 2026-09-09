package com.ails.backend.dto.state;

import com.ails.backend.enums.LearningStateStatus;

import java.time.LocalDateTime;

/**
 * “继续学习”推荐项：按复习优先级/下次复习时间排出的待复习知识点。
 */
public record ContinueLearningItem(
        Long conceptId,
        String conceptName,
        Double mastery,
        Double confidence,
        LearningStateStatus status,
        LocalDateTime nextReviewAt,
        Integer reviewPriority
) {
}
