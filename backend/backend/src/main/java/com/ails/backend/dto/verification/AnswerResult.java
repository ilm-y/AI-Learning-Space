package com.ails.backend.dto.verification;

/**
 * 判题结果 + 更新后的学习状态数值。
 */
public record AnswerResult(
        Boolean isCorrect,
        String feedback,
        Double masteryAfter,
        Double confidenceAfter
) {
}
