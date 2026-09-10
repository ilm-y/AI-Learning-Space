package com.ails.backend.dto.test;

import jakarta.validation.constraints.NotBlank;

/**
 * 提交测试答案请求。
 */
public record AnswerRequest(
        @NotBlank(message = "答案不能为空")
        String userAnswer) {
}
