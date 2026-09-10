package com.ails.backend.dto.agent;

import com.ails.backend.enums.MessageRole;

/**
 * 送给 Agent 的一轮历史消息。
 */
public record ChatHistoryMessage(MessageRole role, String content) {
}
