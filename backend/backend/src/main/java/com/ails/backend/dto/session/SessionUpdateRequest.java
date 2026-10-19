package com.ails.backend.dto.session;

import com.ails.backend.enums.SessionStatus;

/**
 * 更新学习活动：推进状态、改目标、或更新当前焦点。
 * <p>
 * 全部可空，只改给了值的字段。{@code currentFocus} 是这里最常用的一个 ——
 * 「当前卡在哪里」本来就会随对话推进而变，Agent 每轮都可能把它写回来。
 */
public record SessionUpdateRequest(
        /** 可空。非空时按 {@link com.ails.backend.model.SessionResolver} 之外的状态机迁移。 */
        SessionStatus status,

        String title,

        String goal,

        /** 当前焦点。传空字符串表示「清空焦点」，传 null 表示「不改」。 */
        String currentFocus) {
}
