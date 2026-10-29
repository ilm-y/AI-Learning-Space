package com.ails.backend.dto.conversation;

import com.ails.backend.entity.Message;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.enums.MessageType;

import java.time.LocalDateTime;

/**
 * 消息响应。
 */
public record MessageResponse(
        Long id,
        Long conversationId,
        MessageRole role,
        MessageType messageType,
        String content,
        Integer tokens,
        Integer sequenceNo,
        LocalDateTime createdAt,
        boolean truncated) {

    public static MessageResponse from(Message message) {
        if (message == null) {
            return null;
        }
        return new MessageResponse(
                message.getId(), message.getConversationId(), message.getRole(),
                message.getMessageType(), message.getContent(), message.getTokens(),
                message.getSequenceNo(), message.getCreatedAt(),
                Boolean.TRUE.equals(message.getTruncated()));
    }
}
