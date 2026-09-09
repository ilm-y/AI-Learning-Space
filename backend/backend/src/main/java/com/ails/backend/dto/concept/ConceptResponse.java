package com.ails.backend.dto.concept;

import com.ails.backend.entity.Concept;

import java.time.LocalDateTime;

/**
 * 知识点响应体。
 */
public record ConceptResponse(
        Long id,
        Long learningSpaceId,
        String canonicalName,
        String displayName,
        String description,
        Integer difficultyLevel,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
    public static ConceptResponse from(Concept concept) {
        if (concept == null) {
            return null;
        }
        return new ConceptResponse(
                concept.getId(), concept.getLearningSpaceId(), concept.getCanonicalName(),
                concept.getDisplayName(), concept.getDescription(), concept.getDifficultyLevel(),
                concept.getCreatedAt(), concept.getUpdatedAt());
    }
}
