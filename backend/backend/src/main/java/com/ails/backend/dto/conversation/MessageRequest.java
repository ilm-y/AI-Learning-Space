package com.ails.backend.dto.conversation;

import jakarta.validation.constraints.NotBlank;

/**
 * 发送消息请求（M1 仅落库，AI 回复链路后续接 Python Tutor Agent）。
 */
public record MessageRequest(
        @NotBlank(message = "消息内容不能为空") String content
) {
}
