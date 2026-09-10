package com.ails.backend.dto.test;

import com.ails.backend.enums.QuestionType;
import jakarta.validation.constraints.NotNull;

/**
 * 创建测试任务请求。
 * <p>
 * questionText / correctAnswer 是给「出题方」留的口子：将来由 Agent 生成题目时
 * 直接填这两个字段，不传则退化为本地规则生成的简答题。
 */
public record TestTaskRequest(
        @NotNull(message = "知识点不能为空")
        Long conceptId,

        /** 测试嵌入的对话，可不填 */
        Long conversationId,

        /** 不传时默认 SHORT_ANSWER —— 简答题最能暴露真实理解 */
        QuestionType questionType,

        String questionText,

        String correctAnswer,

        /** 简答题评价标准，JSON 字符串 */
        String evaluationRubric) {
}
