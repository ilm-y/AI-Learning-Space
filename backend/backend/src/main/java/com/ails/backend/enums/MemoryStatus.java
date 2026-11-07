package com.ails.backend.enums;

/**
 * Memory 生命周期（memory.status）。
 * <p>
 * AI 推断出的 Memory 默认只能停留在 CANDIDATE，用户明确表达的才可直接 ACTIVE。
 * Memory 不做物理删除，用 ARCHIVED 退役（删除 Memory 不影响 Evidence 与学习历史）。
 * <p>
 * <b>转换矩阵（唯一真相）：</b>
 * <pre>
 *             → CANDIDATE   → ACTIVE   → ARCHIVED
 * CANDIDATE      同态         允许        允许
 * ACTIVE         禁止         同态        允许
 * ARCHIVED       禁止         禁止        同态（仅 DELETE 幂等无操作）
 * </pre>
 * 同态（target == this）在矩阵层视为合法，是否真的写库由调用方决定：
 * {@code MemoryService.archive} 对已归档记忆直接短路，{@code update} 的同态
 * 状态变更不改变最终状态。ARCHIVED 是终态，没有离开该状态的合法迁移。
 */
public enum MemoryStatus {
    CANDIDATE,
    ACTIVE,
    ARCHIVED;

    /**
     * 当前状态是否允许迁移到 {@code target}。
     * <p>
     * 这是 Memory 状态机的唯一定义；{@code MemoryService.applyStatusTransition}
     * 是所有运行时状态写入的唯一入口。创建路径把“无前态”视为 CANDIDATE 基线，
     * 因此 {@code create} 的 ACTIVE 等价于 CANDIDATE → ACTIVE。
     */
    public boolean canTransitionTo(MemoryStatus target) {
        if (target == null) {
            return false;
        }
        if (this == target) {
            return true;
        }
        return switch (this) {
            case CANDIDATE -> target == ACTIVE || target == ARCHIVED;
            case ACTIVE -> target == ARCHIVED;
            case ARCHIVED -> false;
        };
    }
}
