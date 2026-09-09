package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.concept.ConceptRequest;
import com.ails.backend.dto.concept.ConceptResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningSpace;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningSpaceMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;

/**
 * 知识点 CRUD：知识点挂在学习空间下，仅空间所有者可维护。
 */
@Service
@RequiredArgsConstructor
public class ConceptService {

    private final ConceptMapper conceptMapper;
    private final LearningSpaceMapper spaceMapper;

    @Transactional(readOnly = true)
    public List<ConceptResponse> listBySpace(Long userId, Long spaceId) {
        requireOwnedSpace(userId, spaceId);
        return conceptMapper.selectList(new LambdaQueryWrapper<Concept>()
                        .eq(Concept::getLearningSpaceId, spaceId)
                        .orderByAsc(Concept::getId))
                .stream().map(ConceptResponse::from).toList();
    }

    @Transactional
    public ConceptResponse createInSpace(Long userId, Long spaceId, ConceptRequest request) {
        requireOwnedSpace(userId, spaceId);
        Concept concept = new Concept();
        concept.setLearningSpaceId(spaceId);
        concept.setCanonicalName(request.canonicalName().trim());
        concept.setDisplayName(StringUtils.hasText(request.displayName()) ? request.displayName().trim() : null);
        concept.setDescription(request.description());
        concept.setDifficultyLevel(request.difficultyLevel());
        conceptMapper.insert(concept);
        return ConceptResponse.from(concept);
    }

    @Transactional(readOnly = true)
    public ConceptResponse get(Long userId, Long conceptId) {
        return ConceptResponse.from(accessibleConcept(userId, conceptId));
    }

    @Transactional
    public ConceptResponse update(Long userId, Long conceptId, ConceptRequest request) {
        Concept concept = accessibleConcept(userId, conceptId);
        if (StringUtils.hasText(request.canonicalName())) {
            concept.setCanonicalName(request.canonicalName().trim());
        }
        concept.setDisplayName(StringUtils.hasText(request.displayName()) ? request.displayName().trim() : concept.getDisplayName());
        concept.setDescription(request.description());
        if (request.difficultyLevel() != null) {
            concept.setDifficultyLevel(request.difficultyLevel());
        }
        conceptMapper.updateById(concept);
        return ConceptResponse.from(concept);
    }

    @Transactional
    public void delete(Long userId, Long conceptId) {
        Concept concept = accessibleConcept(userId, conceptId);
        // learning_state / learning_evidence / verification_task / concept_relation 等由 FK 级联清理
        conceptMapper.deleteById(concept.getId());
    }

    /**
     * 当前用户可访问的知识点：知识点的学习空间必须属于该用户。
     */
    public Concept accessibleConcept(Long userId, Long conceptId) {
        Concept concept = conceptMapper.selectById(conceptId);
        if (concept == null) {
            throw BusinessException.notFound("知识点不存在");
        }
        requireOwnedSpace(userId, concept.getLearningSpaceId());
        return concept;
    }

    private LearningSpace requireOwnedSpace(Long userId, Long spaceId) {
        LearningSpace space = spaceMapper.selectById(spaceId);
        if (space == null || !space.getUserId().equals(userId)) {
            throw BusinessException.notFound("学习空间不存在");
        }
        return space;
    }
}
