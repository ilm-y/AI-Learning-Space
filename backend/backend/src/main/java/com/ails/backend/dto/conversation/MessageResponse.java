package com.ails.backend.dto.conversation;

import com.ails.backend.entity.Message;
import com.ails.backend.enums.MessageRole;

import java.time.LocalDateTime;

/**
 * 消息响应体。
 */
public record MessageResponse(
        Long id,
        Long conversationId,
        MessageRole role,
        String content,
        Integer tokens,
        LocalDateTime createdAt
) {
    public static MessageResponse from(Message message) {
        if (message == null) {
            return null;
        }
        return new MessageResponse(
                message.getId(), message.getConversationId(), message.getRole(),
                message.getContent(), message.getTokens(), message.getCreatedAt());
    }
}
