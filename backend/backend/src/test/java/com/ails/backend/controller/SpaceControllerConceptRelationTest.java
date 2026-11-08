package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import com.ails.backend.common.GlobalExceptionHandler;
import com.ails.backend.dto.concept.ConceptRelationRow;
import com.ails.backend.entity.Concept;
import com.ails.backend.enums.RelationType;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.ConceptRelationMapper;
import com.ails.backend.security.AuthUser;
import com.ails.backend.service.ConceptRelationService;
import com.ails.backend.service.ConceptService;
import com.ails.backend.service.LearningSpaceService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * HTTP 契约：路径、字段名、跨用户 404。
 * <p>
 * 不启动 Spring 容器、不连接数据库；认证上下文直接写入 SecurityContext。
 */
@ExtendWith(MockitoExtension.class)
class SpaceControllerConceptRelationTest {

    @Mock
    private LearningSpaceService spaceService;
    @Mock
    private ConceptService conceptService;
    @Mock
    private ConceptRelationMapper relationMapper;
    @Mock
    private ConceptMapper conceptMapper;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        ConceptRelationService relationService =
                new ConceptRelationService(relationMapper, conceptMapper, spaceService);
        mockMvc = MockMvcBuilders
                .standaloneSetup(new SpaceController(spaceService, conceptService, relationService))
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();
        authenticateAs(1L);
    }

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    @DisplayName("GET /api/spaces/{id}/concepts/{conceptId}/relations 返回 conceptId/relationType/weight/direction")
    void returnsRelatedConceptsWithDirection() throws Exception {
        when(conceptMapper.selectById(20L)).thenReturn(concept(20L, 1L, "derivative", "导数"));
        when(relationMapper.selectRelated(any(), any(), any(), anyInt()))
                .thenReturn(List.of(row(10L, RelationType.PREREQUISITE, 0.9, "INCOMING")));
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(10L, 1L, "limit", "极限")));

        mockMvc.perform(get("/api/spaces/1/concepts/20/relations")
                        .param("types", "PREREQUISITE,RELATED")
                        .param("limit", "5"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].conceptId").value(10))
                .andExpect(jsonPath("$.data[0].conceptName").value("极限"))
                .andExpect(jsonPath("$.data[0].relationType").value("PREREQUISITE"))
                .andExpect(jsonPath("$.data[0].weight").value(0.9))
                .andExpect(jsonPath("$.data[0].direction").value("INCOMING"));
    }

    @Test
    @DisplayName("跨用户访问他人 Space：404，且不查询关系表")
    void crossUserSpaceReturns404AndQueriesNothing() throws Exception {
        authenticateAs(2L);
        when(spaceService.ownedSpace(2L, 1L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        mockMvc.perform(get("/api/spaces/1/concepts/20/relations"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404));

        verifyNoInteractions(relationMapper);
    }

    private static void authenticateAs(long userId) {
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        new AuthUser(userId, "tester", "token"),
                        null,
                        List.of(new SimpleGrantedAuthority("ROLE_USER"))));
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
