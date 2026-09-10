package com.ails.backend.dto.test;

import com.ails.backend.enums.UnderstandingStatus;

/**
 * 判题结果 + 由这次回答推出的理解状态。
 * <p>
 * 不回传 mastery / confidence 之类的分数：状态由 Evidence → Understanding 链路派生，
 * 想知道依据可以顺着 evidenceId / revisionId 回溯。
 */
public record AnswerResult(
        Boolean isCorrect,
        String feedback,
        Long evidenceId,
        Long revisionId,
        UnderstandingStatus status) {
}
