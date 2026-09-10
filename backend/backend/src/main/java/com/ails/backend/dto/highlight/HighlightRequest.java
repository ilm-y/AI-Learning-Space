package com.ails.backend.dto.highlight;

import com.ails.backend.enums.HighlightType;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * 创建批注。
 * <p>
 * startOffset / endOffset 用来可靠地定位原始消息中的选区，建议一并传。
 */
public record HighlightRequest(
        @NotNull(message = "批注类型不能为空")
        HighlightType markType,

        @NotBlank(message = "选中文本不能为空")
        String selectedText,

        Integer startOffset,

        Integer endOffset,

        String note) {
}
