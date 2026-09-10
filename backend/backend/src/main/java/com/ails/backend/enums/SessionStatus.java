package com.ails.backend.enums;

/**
 * 学习活动状态（learning_session.status）。
 * <p>
 * Session 只是一次学习活动的容器，不承担长期记忆 ——
 * 长期信息必须进 Memory，知识点认知必须进 Learning Understanding。
 */
public enum SessionStatus {
    ACTIVE,
    COMPLETED,
    ABANDONED
}
