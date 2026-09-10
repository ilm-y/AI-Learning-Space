package com.ails.backend.enums;

/**
 * 理解状态。
 * <p>
 * 同时用于 learning_understanding.status（source of truth）与
 * learning_state.status（Materialized Projection），两者取值必须一致。
 * <p>
 * 第一版不引入 mastery / confidence / stability 之类的数值，也不追求
 * {@code mastery = 0.76} 这种表示。
 */
public enum UnderstandingStatus {
    /** 系统无法判断，不强行更新 */
    UNKNOWN,
    /** 接触过，但尚未形成可判断的理解 */
    EXPOSED,
    /** 部分理解 */
    PARTIAL,
    /** 已经能够自己解释 / 演示 */
    DEMONSTRATED,
    /** 存在明确的错误理解 */
    MISCONCEPTION
}
