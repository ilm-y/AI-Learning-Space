package com.ails.backend.dto.space;

import com.ails.backend.enums.SpaceVisibility;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 创建/更新学习空间请求。
 */
public record LearningSpaceRequest(
        @NotBlank(message = "空间名称不能为空")
        @Size(max = 100, message = "空间名称最长 100 字符")
        String name,
        String description,
        SpaceVisibility visibility
) {
}
