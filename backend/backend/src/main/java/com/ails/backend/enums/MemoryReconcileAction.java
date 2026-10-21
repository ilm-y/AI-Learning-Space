package com.ails.backend.enums;

/**
 * Memory Reconciler 的决策（PRD §13）：ADD / UPDATE / IGNORE。
 * <p>
 * 名字里的「建议」很重要：Agent 只提出 action，真正的写入判定在后端 ——
 * Agent 推断出的记忆永远只能落 CANDIDATE，用户手动确认过的 ACTIVE 不会被覆盖。
 */
public enum MemoryReconcileAction {
    ADD,
    UPDATE,
    IGNORE
}
