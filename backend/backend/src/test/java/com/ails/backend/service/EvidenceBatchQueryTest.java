package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.EvidenceSource;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.EvidenceSourceMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * E3-B：跨概念 Evidence 有界查询的作用域、LIMIT 与批量 Sources。
 * <p>
 * 不连接数据库：SQL 形状由 {@link com.ails.backend.mapper.EvidenceMapperBatchSqlTest}
 * 静态校验；这里固定服务层「一次 LIMIT 查询 + 一次 IN 查询」的调用结构，
 * 以及跨用户 / 跨空间 / 空参数的拒绝语义。
 */
@ExtendWith(MockitoExtension.class)
class EvidenceBatchQueryTest {

    private static final long USER_ID = 1L;
    private static final long SPACE_ID = 7L;
    private static final long CONCEPT_A = 10L;
    private static final long CONCEPT_B = 20L;

    @Mock
    private LearningEvidenceMapper evidenceMapper;
    @Mock
    private EvidenceSourceMapper evidenceSourceMapper;
    @Mock
    private ConceptMapper conceptMapper;
    @Mock
    private LearningSpaceService spaceService;

    private EvidenceService service;

    @BeforeEach
    void setUp() {
        service = new EvidenceService(
                evidenceMapper, evidenceSourceMapper, conceptMapper, spaceService);
    }

    @Test
    @DisplayName("一次 LIMIT 查询取证据，一次 IN 查询取全部 sources，不逐条 loadSources")
    void loadsEvidenceWithLimitAndSourcesWithOneInQuery() {
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(CONCEPT_A, SPACE_ID), concept(CONCEPT_B, SPACE_ID)));
        when(evidenceMapper.selectRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A, CONCEPT_B), 5))
                .thenReturn(List.of(evidence(100L, CONCEPT_A), evidence(101L, CONCEPT_B)));
        when(evidenceSourceMapper.selectByEvidenceIds(List.of(100L, 101L)))
                .thenReturn(List.of(
                        source(1L, 100L, EvidenceSourceType.MESSAGE, 900L),
                        source(2L, 101L, EvidenceSourceType.HIGHLIGHT, 901L),
                        source(3L, 101L, EvidenceSourceType.MESSAGE, 902L)));

        List<EvidenceResponse> result = service.listRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A, CONCEPT_B), 5);

        assertThat(result).extracting(EvidenceResponse::id).containsExactly(100L, 101L);
        assertThat(result.get(0).sources()).hasSize(1);
        assertThat(result.get(1).sources())
                .extracting(source -> source.sourceType())
                .containsExactly(EvidenceSourceType.HIGHLIGHT, EvidenceSourceType.MESSAGE);

        // LIMIT 下沉到 SQL，不在应用层切片。
        verify(evidenceMapper).selectRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A, CONCEPT_B), 5);
        // Sources 只批量查一次。
        verify(evidenceSourceMapper).selectByEvidenceIds(List.of(100L, 101L));
        // loadSources 走 selectList；批量路径绝不能回退到逐条查询。
        verify(evidenceSourceMapper, never()).selectList(any());
        verify(evidenceSourceMapper, never()).selectCount(any());
    }

    @Test
    @DisplayName("limit 缺省时使用服务端默认值，超出上限时由服务端封顶")
    void normalizesLimit() {
        when(conceptMapper.selectList(any())).thenReturn(List.of(concept(CONCEPT_A, SPACE_ID)));
        when(evidenceMapper.selectRecentByConcepts(any(), any(), anyList(), anyInt()))
                .thenReturn(List.of());

        service.listRecentByConcepts(USER_ID, SPACE_ID, List.of(CONCEPT_A), null);
        verify(evidenceMapper).selectRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A), EvidenceService.DEFAULT_RECENT_LIMIT);

        service.listRecentByConcepts(USER_ID, SPACE_ID, List.of(CONCEPT_A), 500);
        verify(evidenceMapper).selectRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A), EvidenceService.MAX_RECENT_LIMIT);
    }

    @Test
    @DisplayName("conceptIds 必须属于该 Space：跨空间概念 404，且不查询证据")
    void rejectsConceptFromAnotherSpace() {
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(CONCEPT_B, 999L)));

        assertThatThrownBy(() -> service.listRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A, CONCEPT_B), 5))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verify(evidenceMapper, never()).selectRecentByConcepts(any(), any(), anyList(), anyInt());
    }

    @Test
    @DisplayName("跨用户 Space：404，且不查询概念与证据")
    void rejectsCrossUserSpace() {
        when(spaceService.ownedSpace(USER_ID, SPACE_ID))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() -> service.listRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A), 5))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(conceptMapper, evidenceMapper);
    }

    @Test
    @DisplayName("conceptIds 为空：400，不发任何 SQL")
    void rejectsEmptyConceptIds() {
        assertThatThrownBy(() -> service.listRecentByConcepts(
                USER_ID, SPACE_ID, List.of(), 5))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(conceptMapper, evidenceMapper);
    }

    @Test
    @DisplayName("去重 conceptIds，避免同一概念在 IN 列表中重复")
    void deduplicatesConceptIds() {
        when(conceptMapper.selectList(any())).thenReturn(List.of(concept(CONCEPT_A, SPACE_ID)));
        when(evidenceMapper.selectRecentByConcepts(any(), any(), anyList(), anyInt()))
                .thenReturn(List.of());

        service.listRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A, CONCEPT_A), 5);

        verify(evidenceMapper).selectRecentByConcepts(
                USER_ID, SPACE_ID, List.of(CONCEPT_A), 5);
    }

    private static Concept concept(long id, long spaceId) {
        Concept concept = new Concept();
        concept.setId(id);
        concept.setLearningSpaceId(spaceId);
        concept.setCanonicalName("c" + id);
        return concept;
    }

    private static LearningEvidence evidence(long id, long conceptId) {
        LearningEvidence evidence = new LearningEvidence();
        evidence.setId(id);
        evidence.setUserId(USER_ID);
        evidence.setConceptId(conceptId);
        evidence.setEvidenceType(EvidenceType.OBSERVATION);
        evidence.setObservation("observation-" + id);
        evidence.setCorrectness(EvidenceCorrectness.UNKNOWN);
        return evidence;
    }

    private static EvidenceSource source(
            long id, long evidenceId, EvidenceSourceType type, long sourceId) {
        EvidenceSource source = new EvidenceSource();
        source.setId(id);
        source.setEvidenceId(evidenceId);
        source.setSourceType(type);
        source.setSourceId(sourceId);
        return source;
    }
}
