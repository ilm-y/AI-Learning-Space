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
                conversation.getId(), conversation.getLearningSpaceId(), conversation.getTitle(),
                conversation.getStatus(), conversation.getCreatedAt(), conversation.getUpdatedAt());
    }
}
