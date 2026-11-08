package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.understanding.UnderstandingResponse;
import com.ails.backend.dto.understanding.UnderstandingRevisionResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.enums.UnderstandingStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningUnderstandingMapper;
import com.ails.backend.mapper.UnderstandingRevisionMapper;
import com.ails.backend.model.MisconceptionLifecycle;
import com.ails.backend.utils.JsonUtil;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Understanding / Revision 的可选 Space 校验。
 * <p>
 * {@code learning_understanding} 没有 {@code space_id}，空间归属只能由 concept 推导。
 * 传 {@code spaceId} 时必须拒绝同用户其他 Space 的 conceptId；不传时保持旧行为。
 */
@ExtendWith(MockitoExtension.class)
class UnderstandingServiceSpaceScopeTest {

    private static final long USER_ID = 1L;
    private static final long CONCEPT_ID = 10L;
    private static final long SPACE_ID = 7L;
    private static final long OTHER_SPACE_ID = 8L;

    @Mock
    private LearningUnderstandingMapper understandingMapper;
    @Mock
    private UnderstandingRevisionMapper revisionMapper;
    @Mock
    private ConceptMapper conceptMapper;
    @Mock
    private ConceptService conceptService;

    private UnderstandingService service;

    @BeforeEach
    void setUp() {
        service = new UnderstandingService(
                understandingMapper, revisionMapper, conceptMapper, conceptService);
    }

    @Test
    @DisplayName("spaceId 与 concept 归属一致：正常返回当前快照")
    void getWithMatchingSpaceReturnsSnapshot() {
        when(conceptService.accessibleConcept(USER_ID, CONCEPT_ID)).thenReturn(concept(SPACE_ID));
        when(understandingMapper.selectOne(any())).thenReturn(understanding());

        UnderstandingResponse response = service.get(USER_ID, CONCEPT_ID, SPACE_ID);

        assertThat(response).isNotNull();
        assertThat(response.id()).isEqualTo(500L);
        assertThat(response.conceptId()).isEqualTo(CONCEPT_ID);
    }

    @Test
    @DisplayName("spaceId 与 concept 归属不一致：404，且不读取 Understanding")
    void getWithMismatchedSpaceThrowsNotFound() {
        when(conceptService.accessibleConcept(USER_ID, CONCEPT_ID)).thenReturn(concept(SPACE_ID));

        assertThatThrownBy(() -> service.get(USER_ID, CONCEPT_ID, OTHER_SPACE_ID))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verify(understandingMapper, never()).selectOne(any());
    }

    @Test
    @DisplayName("不传 spaceId：保持旧行为，仍按 concept 的用户归属返回")
    void getWithoutSpaceKeepsLegacyBehavior() {
        when(conceptService.accessibleConcept(USER_ID, CONCEPT_ID)).thenReturn(concept(SPACE_ID));
        when(understandingMapper.selectOne(any())).thenReturn(understanding());

        assertThat(service.get(USER_ID, CONCEPT_ID)).isNotNull();
    }

    @Test
    @DisplayName("Revision：spaceId 不匹配时 404，且不查询版本历史")
    void revisionsWithMismatchedSpaceThrowsNotFound() {
        when(conceptService.accessibleConcept(USER_ID, CONCEPT_ID)).thenReturn(concept(SPACE_ID));

        assertThatThrownBy(() -> service.revisions(USER_ID, CONCEPT_ID, OTHER_SPACE_ID))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verify(revisionMapper, never()).selectList(any());
    }

    @Test
    @DisplayName("Revision：spaceId 匹配时正常查询；没有快照时返回空列表")
    void revisionsWithMatchingSpaceQueriesHistory() {
        when(conceptService.accessibleConcept(USER_ID, CONCEPT_ID)).thenReturn(concept(SPACE_ID));
        when(understandingMapper.selectOne(any())).thenReturn(null);

        List<UnderstandingRevisionResponse> result =
                service.revisions(USER_ID, CONCEPT_ID, SPACE_ID);

        assertThat(result).isEmpty();
        verify(understandingMapper).selectOne(any());
        verify(revisionMapper, never()).selectList(any());
    }

    @Test
    @DisplayName("Revision：不传 spaceId 时保持旧行为")
    void revisionsWithoutSpaceKeepsLegacyBehavior() {
        when(conceptService.accessibleConcept(USER_ID, CONCEPT_ID)).thenReturn(concept(SPACE_ID));
        when(understandingMapper.selectOne(any())).thenReturn(null);

        assertThat(service.revisions(USER_ID, CONCEPT_ID)).isEmpty();
    }

    private static Concept concept(long spaceId) {
        Concept concept = new Concept();
        concept.setId(CONCEPT_ID);
        concept.setLearningSpaceId(spaceId);
        concept.setCanonicalName("c" + CONCEPT_ID);
        return concept;
    }

    private static LearningUnderstanding understanding() {
        LearningUnderstanding understanding = new LearningUnderstanding();
        understanding.setId(500L);
        understanding.setUserId(USER_ID);
        understanding.setConceptId(CONCEPT_ID);
        understanding.setStatus(UnderstandingStatus.PARTIAL);
        understanding.setKnownParts(JsonUtil.fromStringList(List.of()));
        understanding.setUncertainParts(JsonUtil.fromStringList(List.of()));
        understanding.setMisconceptions(MisconceptionLifecycle.serialize(List.of()));
        understanding.setUpdatedAt(LocalDateTime.of(2026, 1, 1, 0, 0));
        return understanding;
    }
}
