package com.ails.backend.enums;

/**
 * Memory 生命周期（memory.status）。
 * <p>
 * AI 推断出的 Memory 默认只能停留在 CANDIDATE，用户明确表达的才可直接 ACTIVE。
 * Memory 不做物理删除，用 ARCHIVED 退役（删除 Memory 不影响 Evidence 与学习历史）。
 */
public enum MemoryStatus {
    CANDIDATE,
    ACTIVE,
    ARCHIVED
}
