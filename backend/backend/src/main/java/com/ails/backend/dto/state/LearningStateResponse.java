package com.ails.backend.dto.state;

import com.ails.backend.entity.LearningState;
import com.ails.backend.enums.UnderstandingStatus;

import java.time.LocalDateTime;

/**
 * 学习状态响应体。
 * <p>
 * LearningState 是 Understanding 的物化投影，因此这里只有状态没有数值分。
 * 想知道「为什么是这个状态」，去看 Understanding 的当前快照与 Revision 历史。
 */
public record LearningStateResponse(
        Long userId,
        Long conceptId,
        String conceptName,
        UnderstandingStatus status,
        LocalDateTime lastEvidenceAt,
        LocalDateTime nextReviewAt,
        Integer reviewPriority,
        LocalDateTime updatedAt) {

    public static LearningStateResponse from(LearningState state, String conceptName) {
        if (state == null) {
            return null;
        }
        return new LearningStateResponse(
                state.getUserId(), state.getConceptId(), conceptName, state.getStatus(),
                state.getLastEvidenceAt(), state.getNextReviewAt(), state.getReviewPriority(),
                state.getUpdatedAt());
    }
}
