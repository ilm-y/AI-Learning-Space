package com.ails.backend.dto.verification;

import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.VerificationTaskStatus;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 验证题响应体。options 为选项列表（选择题时非空）。
 */
public record VerificationTaskResponse(
        Long id,
        Long conceptId,
        String conceptName,
        QuestionType questionType,
        String questionText,
        List<String> options,
        String hint,
        VerificationTaskStatus status,
        LocalDateTime expiredAt,
        LocalDateTime createdAt
) {
}
