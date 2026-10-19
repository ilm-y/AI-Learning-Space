package com.ails.backend.dto.conversation;

import com.ails.backend.entity.Conversation;
import com.ails.backend.enums.ConversationStatus;

import java.time.LocalDateTime;

/**
 * 对话响应体。
 */
public record ConversationResponse(
        Long id,
        Long learningSpaceId,
        /**
         * 所属学习活动。
         * <p>
         * <b>为 null 是正常状态</b>，不是「还没填」：要么用户建了对话还没说话，
         * 要么第一条消息没命中任何 Concept（{@code SessionResolver} 给出 NONE，
         * 闲聊不该产生学习活动）。见 {@code docs/session-design.md} §四。
         */
        Long sessionId,
        String title,
        ConversationStatus status,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
    public static ConversationResponse from(Conversation conversation) {
        if (conversation == null) {
            return null;
        }
        return new ConversationResponse(
                conversation.getId(), conversation.getLearningSpaceId(), conversation.getSessionId(),
                conversation.getTitle(), conversation.getStatus(),
                conversation.getCreatedAt(), conversation.getUpdatedAt());
    }
}
