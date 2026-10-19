package com.ails.backend.enums;

/**
 * Evidence 的类型。见 {@code docs/database&API_design.md} §十四。
 * <p>
 * 这个维度回答的是「这条证据是怎么来的」，与 {@link EvidenceCorrectness}（判断是什么）
 * 正交。它参与 {@code learning_evidence.dedupe_key} 的计算，所以取值必须稳定 ——
 * 改动这里的常量名等于让历史 Evidence 全部换一个身份，会重新产生一批重复行。
 */
public enum EvidenceType {

    /** 用户自己给出的解释（通常来自 MY_UNDERSTANDING 批注）。最强的信号。 */
    USER_EXPLANATION,

    /** 分析器从普通消息里观察到的信号。 */
    OBSERVATION,

    /** 测试回答。 */
    TEST_RESULT,

    /** 用户主动纠正自己或纠正系统的判断（通常来自 MISCONCEPTION_CANDIDATE 批注）。 */
    USER_CORRECTION,

    /** 批注本身携带的信号（如 QUESTION 标记的待跟进）。 */
    HIGHLIGHT_SIGNAL
}
