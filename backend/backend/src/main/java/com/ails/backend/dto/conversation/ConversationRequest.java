package com.ails.backend.dto.conversation;

import jakarta.validation.constraints.Size;

/**
 * 新建对话请求。标题可为空（AI 自动总结）。
 */
public record ConversationRequest(
        @Size(max = 200, message = "标题最长 200 字符")
        String title
) {
}
