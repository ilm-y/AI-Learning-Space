package com.ails.backend.dto.understanding;

import com.ails.backend.entity.UnderstandingRevision;
import com.ails.backend.enums.UnderstandingStatus;
import com.ails.backend.utils.JsonUtil;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 理解历史版本响应 —— 用来回答「用户什么时候开始理解这个概念」「为什么系统改了判断」。
 */
public record UnderstandingRevisionResponse(
        Long id,
        Integer revisionNo,
        String summary,
        List<String> knownParts,
        List<String> misconceptions,
        List<String> uncertainParts,
        String userExplanation,
        UnderstandingStatus status,
        Long triggerEvidenceId,
        LocalDateTime createdAt) {

    public static UnderstandingRevisionResponse from(UnderstandingRevision revision) {
        if (revision == null) {
            return null;
        }
        return new UnderstandingRevisionResponse(
                revision.getId(), revision.getRevisionNo(), revision.getSummary(),
                JsonUtil.toStringList(revision.getKnownParts()),
                JsonUtil.toStringList(revision.getMisconceptions()),
                JsonUtil.toStringList(revision.getUncertainParts()),
                revision.getUserExplanation(), revision.getStatus(),
                revision.getTriggerEvidenceId(), revision.getCreatedAt());
    }
}
