package com.ails.backend.enums;

/**
 * 主动验证策略（PRD §16）。
 * <p>
 * 只有 {@link #ASK_NOW} 会在当前对话里插入 Test Card；{@link #ASK_LATER}
 * 只记录决策，不立刻打断用户。第一版没有调度表，所以 ASK_LATER 不会自动变成
 * 一个未来的任务 —— 这条边界写在这里，避免读代码的人以为「稍后」已经实现了。
 */
public enum VerificationAction {
    SKIP,
    ASK_NOW,
    ASK_LATER
}
