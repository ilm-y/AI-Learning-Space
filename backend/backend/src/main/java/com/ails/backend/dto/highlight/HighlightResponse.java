package com.ails.backend.dto.highlight;

import com.ails.backend.entity.Highlight;
import com.ails.backend.enums.HighlightType;

import java.time.LocalDateTime;

/**
 * 高亮响应体。
 */
public record HighlightResponse(
        Long id,
        Long messageId,
        HighlightType type,
        String selectedText,
        String note,
        LocalDateTime createdAt
) {
    public static HighlightResponse from(Highlight highlight) {
        if (highlight == null) {
            return null;
        }
        return new HighlightResponse(
                highlight.getId(), highlight.getMessageId(), highlight.getType(),
                highlight.getSelectedText(), highlight.getNote(), highlight.getCreatedAt());
    }
}
