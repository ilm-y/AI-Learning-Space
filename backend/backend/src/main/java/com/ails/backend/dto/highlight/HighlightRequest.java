package com.ails.backend.dto.highlight;

import com.ails.backend.enums.HighlightType;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * 新建高亮请求。
 */
public record HighlightRequest(
        @NotNull(message = "高亮类型不能为空") HighlightType type,
        @NotBlank(message = "选中文本不能为空") String selectedText,
        String note
) {
}
