package com.ails.backend.enums;

/**
 * 「现在最该关注什么」的推荐理由。
 * <p>
 * 依据来自 Understanding 的当前状态与证据时间，而不是数值阈值。
 */
public enum AttentionReason {

    /** 当前理解被判定为误解，最优先纠正 */
    MISCONCEPTION,

    /** 误区已经出现反向的正确证据，可能正在解除 —— 仍然要关注，但不用那么急 */
    MISCONCEPTION_RESOLVING,

    /** 长时间没有新证据，理解可能已经过期 */
    LONG_UNVERIFIED,

    /** 到了复习时间，但状态仍未达到 DEMONSTRATED */
    NEEDS_REVIEW
}
