package com.ails.backend.dto.highlight;

import com.ails.backend.entity.Highlight;
import com.ails.backend.enums.HighlightType;

import java.time.LocalDateTime;

/**
 * 批注响应。
 */
public record HighlightResponse(
        Long id,
        Long messageId,
        HighlightType markType,
        String selectedText,
        Integer startOffset,
        Integer endOffset,
        String note,
        LocalDateTime createdAt) {

    public static HighlightResponse from(Highlight highlight) {
        if (highlight == null) {
            return null;
        }
        return new HighlightResponse(
                highlight.getId(), highlight.getMessageId(), highlight.getMarkType(),
                highlight.getSelectedText(), highlight.getStartOffset(), highlight.getEndOffset(),
                highlight.getNote(), highlight.getCreatedAt());
    }
}
