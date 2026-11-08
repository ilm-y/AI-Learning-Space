package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.concept.ConceptRelationResponse;
import com.ails.backend.dto.concept.ConceptRelationRow;
import com.ails.backend.entity.Concept;
import com.ails.backend.enums.RelationType;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.ConceptRelationMapper;
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
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 概念关系查询的作用域、方向与边界。
 * <p>
 * 不连接数据库：SQL 的空间/软删条件由 {@link com.ails.backend.mapper.ConceptRelationMapperSqlTest}
 * 静态校验；这里固定服务层的第二道防线与对外契约。
 */
@ExtendWith(MockitoExtension.class)
class ConceptRelationServiceTest {

    private static final long USER_ID = 1L;
    private static final long SPACE_ID = 1L;
    private static final long DERIVATIVE_ID = 20L;
    private static final long LIMIT_ID = 10L;
    private static final long OTHER_SPACE_CONCEPT_ID = 99L;

    @Mock
    private ConceptRelationMapper relationMapper;
    @Mock
    private ConceptMapper conceptMapper;
    @Mock
    private LearningSpaceService spaceService;

    private ConceptRelationService service;

    @BeforeEach
    void setUp() {
        service = new ConceptRelationService(relationMapper, conceptMapper, spaceService);
    }

    @Test
    @DisplayName("PREREQUISITE 方向假设：source 是 target 的前置，从 target 查询返回 INCOMING")
    void prerequisiteDirectionIsSourceToTarget() {
        when(conceptMapper.selectById(DERIVATIVE_ID)).thenReturn(concept(DERIVATIVE_ID, SPACE_ID, "derivative", "导数"));
        when(relationMapper.selectRelated(
                eq(DERIVATIVE_ID), eq(SPACE_ID),
                eq(List.of(RelationType.PREREQUISITE, RelationType.RELATED)), eq(5)))
                .thenReturn(List.of(row(LIMIT_ID, RelationType.PREREQUISITE, 0.9, "INCOMING")));
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(LIMIT_ID, SPACE_ID, "limit", "极限")));

        List<ConceptRelationResponse> result = service.listRelated(
                USER_ID, SPACE_ID, DERIVATIVE_ID, null, null);

        // 方向不是猜测：这个断言固定了“极限(source) -> 导数(target) 是 PREREQUISITE”。
        // 若未来把方向改成 target 是前置，这里会失败，Agent 的扩展筛选也必须一起改。
        assertThat(result).containsExactly(
                new ConceptRelationResponse(LIMIT_ID, "极限", RelationType.PREREQUISITE, 0.9, "INCOMING"));
    }

    @Test
    @DisplayName("跨 Space 的关系在回查时被排除（SQL 已过滤，这里固定第二道防线）")
    void crossSpaceRelationIsExcluded() {
        when(conceptMapper.selectById(DERIVATIVE_ID)).thenReturn(concept(DERIVATIVE_ID, SPACE_ID, "derivative", "导数"));
        when(relationMapper.selectRelated(any(), any(), any(), anyInt()))
                .thenReturn(List.of(row(OTHER_SPACE_CONCEPT_ID, RelationType.RELATED, 1.0, "INCOMING")));
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(OTHER_SPACE_CONCEPT_ID, 2L, "other", "其他空间概念")));

        List<ConceptRelationResponse> result = service.listRelated(
                USER_ID, SPACE_ID, DERIVATIVE_ID, null, null);

        assertThat(result).isEmpty();
    }

    @Test
    @DisplayName("跨用户空间：在查询关系前返回 404，不泄露关系数据")
    void crossUserSpaceIsRejectedBeforeQueryingRelations() {
        when(spaceService.ownedSpace(USER_ID, SPACE_ID))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() -> service.listRelated(
                USER_ID, SPACE_ID, DERIVATIVE_ID, null, null))
                .isInstanceOfSatisfying(BusinessException.class, ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(relationMapper);
    }

    @Test
    @DisplayName("路径中的概念不属于该 Space：404，不查询关系")
    void conceptFromAnotherSpaceOnPathIsRejected() {
        when(conceptMapper.selectById(DERIVATIVE_ID))
                .thenReturn(concept(DERIVATIVE_ID, 2L, "derivative", "导数"));

        assertThatThrownBy(() -> service.listRelated(
                USER_ID, SPACE_ID, DERIVATIVE_ID, null, null))
                .isInstanceOfSatisfying(BusinessException.class, ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(relationMapper);
    }

    @Test
    @DisplayName("默认类型为 PREREQUISITE + RELATED，limit 被服务端上限裁剪")
    void defaultsToPrerequisiteAndRelatedAndClampsLimit() {
        when(conceptMapper.selectById(DERIVATIVE_ID)).thenReturn(concept(DERIVATIVE_ID, SPACE_ID, "derivative", "导数"));
        when(relationMapper.selectRelated(
                eq(DERIVATIVE_ID), eq(SPACE_ID),
                eq(List.of(RelationType.PREREQUISITE, RelationType.RELATED)), eq(ConceptRelationService.MAX_LIMIT)))
                .thenReturn(List.of());

        assertThat(service.listRelated(USER_ID, SPACE_ID, DERIVATIVE_ID, null, 500)).isEmpty();

        verify(relationMapper).selectRelated(
                eq(DERIVATIVE_ID), eq(SPACE_ID),
                eq(List.of(RelationType.PREREQUISITE, RelationType.RELATED)),
                eq(ConceptRelationService.MAX_LIMIT));
    }

    @Test
    @DisplayName("显式类型过滤被尊重：只请求 RELATED 时不会带上 PREREQUISITE")
    void explicitTypeFilterIsRespected() {
        when(conceptMapper.selectById(DERIVATIVE_ID)).thenReturn(concept(DERIVATIVE_ID, SPACE_ID, "derivative", "导数"));
        when(relationMapper.selectRelated(
                eq(DERIVATIVE_ID), eq(SPACE_ID), eq(List.of(RelationType.RELATED)), eq(5)))
                .thenReturn(List.of());

        assertThat(service.listRelated(
                USER_ID, SPACE_ID, DERIVATIVE_ID, List.of(RelationType.RELATED), null)).isEmpty();

        verify(relationMapper).selectRelated(
                eq(DERIVATIVE_ID), eq(SPACE_ID), eq(List.of(RelationType.RELATED)), eq(5));
    }

    @Test
    @DisplayName("PART_OF / CONTRAST 不是默认开启的类型：显式请求也返回 400")
    void unsupportedRelationTypeIsRejected() {
        when(conceptMapper.selectById(DERIVATIVE_ID)).thenReturn(concept(DERIVATIVE_ID, SPACE_ID, "derivative", "导数"));

        assertThatThrownBy(() -> service.listRelated(
                USER_ID, SPACE_ID, DERIVATIVE_ID, List.of(RelationType.PART_OF), null))
                .isInstanceOfSatisfying(BusinessException.class, ex -> assertThat(ex.getCode()).isEqualTo(400));

        verify(relationMapper, never()).selectRelated(any(), any(), any(), anyInt());
    }

    @Test
    @DisplayName("关联概念已软删/查不到：跳过，不返回空洞的关系")
    void missingRelatedConceptIsSkipped() {
        when(conceptMapper.selectById(DERIVATIVE_ID)).thenReturn(concept(DERIVATIVE_ID, SPACE_ID, "derivative", "导数"));
        when(relationMapper.selectRelated(any(), any(), any(), anyInt()))
                .thenReturn(List.of(row(LIMIT_ID, RelationType.PREREQUISITE, 0.9, "INCOMING")));
        when(conceptMapper.selectList(any())).thenReturn(List.of());

        assertThat(service.listRelated(USER_ID, SPACE_ID, DERIVATIVE_ID, null, null)).isEmpty();
    }

    private static Concept concept(long id, long spaceId, String canonicalName, String displayName) {
        Concept concept = new Concept();
        concept.setId(id);
        concept.setLearningSpaceId(spaceId);
        concept.setCanonicalName(canonicalName);
        concept.setDisplayName(displayName);
        return concept;
    }

    private static ConceptRelationRow row(long relatedId, RelationType type, Double weight, String direction) {
        ConceptRelationRow row = new ConceptRelationRow();
        row.setRelatedConceptId(relatedId);
        row.setRelationType(type);
        row.setWeight(weight);
        row.setDirection(direction);
        return row;
    }
}
