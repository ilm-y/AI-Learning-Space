package com.ails.backend.dto.understanding;

import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceFollowUpStatus;
import com.ails.backend.enums.EvidenceType;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 证据响应。
 * <p>
 * reasoningQuality / specificity / aiConfidence 是内部分析信号，第一版不直接展示给用户。
 */
public record EvidenceResponse(
        Long id,
        Long conceptId,
        Long understandingId,
        EvidenceType evidenceType,
        String observation,
        EvidenceCorrectness correctness,
        Float reasoningQuality,
        Float specificity,
        Float aiConfidence,
        Boolean needFollowUp,
        EvidenceFollowUpStatus followUpStatus,
        LocalDateTime createdAt,
        List<EvidenceSourceResponse> sources) {

    public static EvidenceResponse from(LearningEvidence evidence) {
        return from(evidence, List.of());
    }

    public static EvidenceResponse from(LearningEvidence evidence, List<EvidenceSourceResponse> sources) {
        if (evidence == null) {
            return null;
        }
        return new EvidenceResponse(
                evidence.getId(), evidence.getConceptId(), evidence.getUnderstandingId(),
                evidence.getEvidenceType(),
                evidence.getObservation(), evidence.getCorrectness(), evidence.getReasoningQuality(),
                evidence.getSpecificity(), evidence.getAiConfidence(), evidence.getNeedFollowUp(),
                evidence.getFollowUpStatus(), evidence.getCreatedAt(),
                sources == null ? List.of() : sources);
    }
}
