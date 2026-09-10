package com.ails.backend.dto.test;

import com.ails.backend.entity.TestTask;
import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.TestTaskStatus;
import com.ails.backend.utils.JsonUtil;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 验证题响应体。
 * <p>
 * 注意：不下发 correctAnswer —— 答案只在服务端参与判分。
 */
public record TestTaskResponse(
        Long id,
        Long conceptId,
        String conceptName,
        Long conversationId,
        QuestionType questionType,
        String questionText,
        List<String> options,
        String hint,
        TestTaskStatus status,
        LocalDateTime expiredAt,
        LocalDateTime createdAt) {

    public static TestTaskResponse from(TestTask task, String conceptName) {
        if (task == null) {
            return null;
        }
        return new TestTaskResponse(
                task.getId(), task.getConceptId(), conceptName, task.getConversationId(),
                task.getQuestionType(), task.getQuestionText(), JsonUtil.toStringList(task.getOptions()),
                task.getHint(), task.getStatus(), task.getExpiredAt(), task.getCreatedAt());
    }
}
