package com.ails.backend.dto.memory;

import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * 创建 / 更新一条 Memory。
 * <p>
 * 用户手动创建的一律落 ACTIVE —— 这是他自己说的，不需要再确认。
 * Agent 推断出来的内容默认只能停在 CANDIDATE，要用户点过才升到 ACTIVE。
 * 更新时可选传 status 来推进生命周期。
 * <p>
 * <b>作用域（scope_type）与 scope_key 不在这里。</b> 它们由 {@code learningSpaceId} /
 * {@code conceptId} 推导：给了 conceptId 就是 CONCEPT（空间取自该知识点），
 * 只给 spaceId 就是 SPACE，两个都不给就是 GLOBAL。
 * 让调用方直接传 scope_type 只会多出一种「和两个 id 打架」的非法组合，
 * 而作用域恰恰是检索可见性的唯一依据，不能有第二种真相。
 */
public record MemoryRequest(
        Long learningSpaceId,

        Long conceptId,

        @NotNull(message = "记忆类型不能为空")
        MemoryType memoryType,

        /**
         * 同作用域内的记忆标识，如 {@code os/teaching-style}、{@code profile}。
         * <p>
         * 可空。缺省时由 content 的哈希兜底 —— 这样「同样内容重复提交」仍然是同一条记忆，
         * 唯一约束 {@code uk_memory_identity} 不会形同虚设。
         * 想在同一个作用域里存多条内容相同的记忆，就必须显式给不同的 memoryKey。
         */
        String memoryKey,

        @NotBlank(message = "记忆内容不能为空")
        String content,

        Float importance,

        /** 仅更新时生效；创建时忽略，固定为 ACTIVE */
        MemoryStatus status) {
}
