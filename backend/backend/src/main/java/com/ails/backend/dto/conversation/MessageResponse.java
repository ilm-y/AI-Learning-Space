package com.ails.backend.dto.conversation;

import com.ails.backend.entity.Message;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.enums.MessageType;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 消息响应。
 * <p>
 * {@code citations} 是助手回复的结构化来源。它们是**附加字段**：旧客户端忽略即可，
 * 用户消息、TEST / EVENT 消息以及引用功能上线前的历史消息都返回空数组。
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
        boolean truncated,
        List<CitationResponse> citations) {

    public static MessageResponse from(Message message) {
        return from(message, List.of());
    }

    public static MessageResponse from(Message message, List<CitationResponse> citations) {
        if (message == null) {
            return null;
        }
        return new MessageResponse(
                message.getId(), message.getConversationId(), message.getRole(),
                message.getMessageType(), message.getContent(), message.getTokens(),
                message.getSequenceNo(), message.getCreatedAt(),
                Boolean.TRUE.equals(message.getTruncated()),
                citations == null ? List.of() : List.copyOf(citations));
    }
}
