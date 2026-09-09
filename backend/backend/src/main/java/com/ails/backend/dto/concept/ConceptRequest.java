package com.ails.backend.dto.concept;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 创建/更新知识点请求。
 */
public record ConceptRequest(
        @NotBlank(message = "规范名称不能为空")
        @Size(max = 255, message = "规范名称最长 255 字符")
        String canonicalName,
        @Size(max = 100, message = "显示名称最长 100 字符")
        String displayName,
        String description,
        @Min(value = 1, message = "难度等级需在 1~5")
        @Max(value = 5, message = "难度等级需在 1~5")
        Integer difficultyLevel
) {
}
