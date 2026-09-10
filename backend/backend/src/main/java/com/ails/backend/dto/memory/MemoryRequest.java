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
 */
public record MemoryRequest(
        Long learningSpaceId,

        Long conceptId,

        @NotNull(message = "记忆类型不能为空")
        MemoryType memoryType,

        /** 层次路径，如 os/hardware-first */
        String path,

        @NotBlank(message = "记忆内容不能为空")
        String content,

        Float importance,

        /** 仅更新时生效；创建时忽略，固定为 ACTIVE */
        MemoryStatus status) {
}
