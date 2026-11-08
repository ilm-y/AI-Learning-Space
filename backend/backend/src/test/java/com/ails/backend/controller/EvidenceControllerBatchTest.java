package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import com.ails.backend.common.GlobalExceptionHandler;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.dto.understanding.EvidenceSourceResponse;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.security.AuthUser;
import com.ails.backend.service.EvidenceService;
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

import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * HTTP 契约：{@code GET /api/evidences?spaceId=&conceptIds=&limit=}。
 * <p>
 * 不启动 Spring 容器、不连接数据库；认证上下文直接写入 SecurityContext。
 * 这里固定路径、参数拆分方式与错误码，避免 Agent 客户端与服务端契约漂移。
 */
@ExtendWith(MockitoExtension.class)
class EvidenceControllerBatchTest {

    @Mock
    private EvidenceService evidenceService;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders
                .standaloneSetup(new EvidenceController(evidenceService))
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();
        authenticateAs(1L);
    }

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    @DisplayName("conceptIds 支持逗号分隔，limit 透传，sources 原样返回")
    void returnsBoundedEvidenceList() throws Exception {
        when(evidenceService.listRecentByConcepts(1L, 7L, List.of(10L, 20L), 5))
                .thenReturn(List.of(
                        EvidenceResponse.from(
                                evidence(100L, 10L),
                                List.of(new EvidenceSourceResponse(EvidenceSourceType.MESSAGE, 900L))),
                        EvidenceResponse.from(evidence(101L, 20L), List.of())));

        mockMvc.perform(get("/api/evidences")
                        .param("spaceId", "7")
                        .param("conceptIds", "10,20")
                        .param("limit", "5"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].id").value(100))
                .andExpect(jsonPath("$.data[0].conceptId").value(10))
                .andExpect(jsonPath("$.data[0].sources[0].sourceType").value("MESSAGE"))
                .andExpect(jsonPath("$.data[0].sources[0].sourceId").value(900))
                .andExpect(jsonPath("$.data[1].id").value(101));

        verify(evidenceService).listRecentByConcepts(1L, 7L, List.of(10L, 20L), 5);
    }

    @Test
    @DisplayName("limit 缺省时传 null，由服务端决定默认值")
    void limitIsOptional() throws Exception {
        when(evidenceService.listRecentByConcepts(1L, 7L, List.of(10L), null))
                .thenReturn(List.of());

        mockMvc.perform(get("/api/evidences")
                        .param("spaceId", "7")
                        .param("conceptIds", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data").isArray());

        verify(evidenceService).listRecentByConcepts(1L, 7L, List.of(10L), null);
    }

    @Test
    @DisplayName("跨用户 Space：404，不返回任何证据")
    void crossUserSpaceReturns404() throws Exception {
        authenticateAs(2L);
        when(evidenceService.listRecentByConcepts(2L, 7L, List.of(10L), 5))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        mockMvc.perform(get("/api/evidences")
                        .param("spaceId", "7")
                        .param("conceptIds", "10")
                        .param("limit", "5"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404));
    }

    private static void authenticateAs(long userId) {
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        new AuthUser(userId, "tester", "token"),
                        null,
                        List.of(new SimpleGrantedAuthority("ROLE_USER"))));
    }

    private static LearningEvidence evidence(long id, long conceptId) {
        LearningEvidence evidence = new LearningEvidence();
        evidence.setId(id);
        evidence.setConceptId(conceptId);
        evidence.setEvidenceType(EvidenceType.OBSERVATION);
        evidence.setObservation("observation-" + id);
        evidence.setCorrectness(EvidenceCorrectness.UNKNOWN);
        return evidence;
    }
}
