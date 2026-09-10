package com.ails.backend.enums;

/**
 * 测试题型（test_task.question_type）。
 * <p>
 * 第一版只做这三种。简答题不能依赖 {@code answer == correct_answer}，
 * 应由 Agent 结合 evaluation_rubric 做语义评价。
 */
public enum QuestionType {
    SINGLE_CHOICE,
    TRUE_FALSE,
    SHORT_ANSWER
}
