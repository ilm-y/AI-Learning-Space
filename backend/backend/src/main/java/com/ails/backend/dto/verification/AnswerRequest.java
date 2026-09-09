package com.ails.backend.dto.verification;

import jakarta.validation.constraints.NotBlank;

/**
 * 提交验证题答案请求。
 */
public record AnswerRequest(
        @NotBlank(message = "答案不能为空") String userAnswer
) {
}
