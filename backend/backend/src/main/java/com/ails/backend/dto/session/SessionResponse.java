package com.ails.backend.dto.session;

import com.ails.backend.entity.LearningSession;
import com.ails.backend.enums.SessionStatus;

import java.time.LocalDateTime;

/**
 * 学习活动响应体。
 * <p>
 * {@code goal} 与 {@code currentFocus} 分两个字段而不是一个：前者是这一阶段最终要解决什么，
 * 后者是当前卡在哪里。Agent 的上下文里两个都要用，合并成一个就分不出「长期目标」
 * 和「此刻的卡点」了。
 */
public record SessionResponse(
        Long id,
        Long learningSpaceId,
        String title,
        String goal,
        String currentFocus,
        SessionStatus status,
        LocalDateTime startedAt,
        LocalDateTime lastActiveAt,
        LocalDateTime completedAt) {

    public static SessionResponse from(LearningSession session) {
        if (session == null) {
            return null;
        }
        return new SessionResponse(
                session.getId(), session.getLearningSpaceId(), session.getTitle(),
                session.getGoal(), session.getCurrentFocus(), session.getStatus(),
                session.getStartedAt(), session.getLastActiveAt(), session.getCompletedAt());
    }
}
