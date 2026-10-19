package com.ails.backend.enums;

/**
 * 学习活动的状态，见 {@code docs/session-design.md} §九。
 * <p>
 * 关键区分：{@link #PAUSED} 不是「学完了」也不是「放弃了」。
 * 一周没碰的 Session 不该被自动当成完成，也不该被直接复活 ——
 * 它只是暂时停下，学习目标还没达成。
 */
public enum SessionStatus {

    /** 当前正在进行。 */
    ACTIVE,

    /** 暂时停下，但学习目标仍未完成。 */
    PAUSED,

    /** 学习目标已达成。 */
    COMPLETED,

    /** 用户明确放弃这条学习目标。 */
    ABANDONED
}
