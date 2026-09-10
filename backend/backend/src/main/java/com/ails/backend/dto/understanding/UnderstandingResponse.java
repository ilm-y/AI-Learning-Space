package com.ails.backend.dto.understanding;

import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.enums.UnderstandingStatus;
import com.ails.backend.utils.JsonUtil;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 当前理解快照响应。
 * <p>
 * 这是「用户现在怎么理解这个概念」的对外表示，也是学习状态的 source of truth。
 */
public record UnderstandingResponse(
        Long id,
        Long conceptId,
        String conceptName,
        String summary,
        List<String> knownParts,
        List<String> misconceptions,
        List<String> uncertainParts,
        String latestUserExplanation,
        UnderstandingStatus status,
        Long currentRevisionId,
        LocalDateTime updatedAt) {

    public static UnderstandingResponse from(LearningUnderstanding understanding, String conceptName) {
        if (understanding == null) {
            return null;
        }
        return new UnderstandingResponse(
                understanding.getId(), understanding.getConceptId(), conceptName,
                understanding.getSummary(),
                JsonUtil.toStringList(understanding.getKnownParts()),
                JsonUtil.toStringList(understanding.getMisconceptions()),
                JsonUtil.toStringList(understanding.getUncertainParts()),
                understanding.getLatestUserExplanation(), understanding.getStatus(),
                understanding.getCurrentRevisionId(), understanding.getUpdatedAt());
    }
}
