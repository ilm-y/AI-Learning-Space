package com.ails.backend.enums;

/**
 * Outbox 事件状态。
 *
 * <p>{@code PROCESSING} 不是终态：租约到期后会被回收为 {@code PENDING}，
 * 对应进程崩溃/重启。{@code DONE} 与 {@code DEAD} 才是终态。</p>
 */
public enum OutboxEventStatus {
    PENDING,
    PROCESSING,
    DONE,
    DEAD
}
