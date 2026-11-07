package com.ails.backend.dto.memory;

import com.ails.backend.enums.MemoryHybridStatus;

import java.util.List;

/**
 * 混合检索响应。
 *
 * <p>{@code status} 必须与 {@code items} 一起使用：{@code UNAVAILABLE} 时列表为空，
 * 但它表示「检索不可用」，不是「没有相关记忆」。</p>
 */
public record HybridMemorySearchResponse(
        MemoryHybridStatus status,
        List<HybridMemoryHit> items,
        List<String> warnings,
        long elapsedMs) {
}
