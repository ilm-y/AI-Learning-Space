package com.ails.backend.enums;

/**
 * 批注类型（highlight.mark_type）。
 * <p>
 * 并不是每一种批注都会变成 Evidence：只有 MY_UNDERSTANDING /
 * MISCONCEPTION_CANDIDATE / QUESTION 会进入证据链，IMPORTANT 与 CONCEPT_LINK 不会。
 * 用户随手点十几个「重要」不等于用户理解了，否则证据会变脏。
 */
public enum HighlightType {
    /** 普通标记，不产生 Evidence */
    IMPORTANT,
    /** 不确定性信号，产生 Evidence */
    QUESTION,
    /** 用户自己的解释，最强信号，产生 Evidence */
    MY_UNDERSTANDING,
    /** 候选误区，产生 Evidence */
    MISCONCEPTION_CANDIDATE,
    /** 概念关联，只建 Concept 关联，不产生 Evidence */
    CONCEPT_LINK
}
