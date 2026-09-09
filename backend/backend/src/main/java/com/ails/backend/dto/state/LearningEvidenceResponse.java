package com.ails.backend.dto.state;

import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;

import java.time.LocalDateTime;

/**
 * 证据链响应体。
 */
public record LearningEvidenceResponse(
        Long id,
        Long conceptId,
        EvidenceSourceType sourceType,
        Long sourceId,
        String observation,
        EvidenceCorrectness correctness,
        Double weight,
        Double aiConfidence,
        LocalDateTime createdAt
) {
    public static LearningEvidenceResponse from(LearningEvidence evidence) {
        if (evidence == null) {
            return null;
        }
        return new LearningEvidenceResponse(
                evidence.getId(), evidence.getConceptId(), evidence.getSourceType(),
                evidence.getSourceId(), evidence.getObservation(), evidence.getCorrectness(),
                evidence.getWeight(), evidence.getAiConfidence(), evidence.getCreatedAt());
    }
}
