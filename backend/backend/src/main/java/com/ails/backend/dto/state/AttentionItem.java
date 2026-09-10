package com.ails.backend.dto.state;

import com.ails.backend.enums.AttentionReason;
import com.ails.backend.enums.UnderstandingStatus;

import java.time.LocalDateTime;

/**
 * 「现在最该关注什么」的推荐项。
 * <p>
 * 取代了早期的 Dashboard 聚合接口：不做分数看板，只回答「下一步该碰哪个概念，为什么」。
 */
public record AttentionItem(
        Long conceptId,
        String conceptName,
        UnderstandingStatus status,
        AttentionReason reason,
        LocalDateTime lastEvidenceAt,
        LocalDateTime nextReviewAt,
        Integer reviewPriority) {
}
