package com.ails.backend.enums;

/**
 * 主动验证的触发原因（PRD §16）。
 * <p>
 * 它回答的是「为什么值得验证」，与 {@link VerificationAction}（现在要不要打断）
 * 正交：同一个触发原因可以在不同情境下得到不同的策略。
 */
public enum VerificationTrigger {
    NEW_CONCEPT,
    CONCEPT_CONFLICT,
    REPEATED_ERROR,
    CONTRADICTORY_EVIDENCE,
    OVERCONFIDENCE,
    LONG_UNVERIFIED
}
