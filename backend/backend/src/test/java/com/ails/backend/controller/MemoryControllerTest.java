package com.ails.backend.controller;

import com.ails.backend.common.GlobalExceptionHandler;
import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import com.ails.backend.security.AuthUser;
import com.ails.backend.service.ConceptService;
import com.ails.backend.service.LearningSpaceService;
import com.ails.backend.service.MemoryService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.MediaType;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * T1 / T2 / T22：走 HTTP 层验证非法转换的 409 契约与非法枚举的 400 契约。
 * 不启动 Spring 容器、不连接数据库；认证上下文直接写入 SecurityContext。
 */
@ExtendWith(MockitoExtension.class)
class MemoryControllerTest {

    @Mock
    private MemoryMapper memoryMapper;
    @Mock
    private MemorySourceMapper memorySourceMapper;
    @Mock
    private LearningSpaceService spaceService;
    @Mock
    private ConceptService conceptService;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        MemoryService memoryService = new MemoryService(
                memoryMapper, memorySourceMapper, spaceService, conceptService);
        mockMvc = MockMvcBuilders
                .standaloneSetup(new MemoryController(memoryService))
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        new AuthUser(1L, "tester", "token"),
                        null,
                        List.of(new SimpleGrantedAuthority("ROLE_USER"))));
        lenient().when(memoryMapper.selectCount(any())).thenReturn(0L);
        // 红色阶段需要让旧实现真正走到 updateById：否则默认 0 会让旧实现也返回 409。
        lenient().when(memoryMapper.updateById(any(Memory.class))).thenReturn(1);
    }

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    @DisplayName("T1 ARCHIVED → ACTIVE：PATCH 返回 409，响应体带可区分原因")
    void t1_archivedToActiveReturns409() throws Exception {
        when(memoryMapper.selectById(42L)).thenReturn(memory(MemoryStatus.ARCHIVED));

        mockMvc.perform(patch("/api/memories/42")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"memoryType":"LEARNING","content":"new","status":"ACTIVE"}
                                """))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value(409))
                .andExpect(jsonPath("$.message").value(
                        org.hamcrest.Matchers.containsString("ARCHIVED → ACTIVE")));

        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T2 ACTIVE → CANDIDATE：PATCH 返回 409，响应体带可区分原因")
    void t2_activeToCandidateReturns409() throws Exception {
        when(memoryMapper.selectById(42L)).thenReturn(memory(MemoryStatus.ACTIVE));

        mockMvc.perform(patch("/api/memories/42")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"memoryType":"LEARNING","content":"new","status":"CANDIDATE"}
                                """))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value(409))
                .andExpect(jsonPath("$.message").value(
                        org.hamcrest.Matchers.containsString("ACTIVE → CANDIDATE")));

        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T22 非法枚举值：400，不进入业务层")
    void t22_invalidStatusEnumReturns400() throws Exception {
        mockMvc.perform(patch("/api/memories/42")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"memoryType":"LEARNING","content":"new","status":"RESURRECTED"}
                                """))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        verify(memoryMapper, never()).selectById(any());
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    private static Memory memory(MemoryStatus status) {
        Memory memory = new Memory();
        memory.setId(42L);
        memory.setUserId(1L);
        memory.setScopeType(MemoryScopeType.GLOBAL);
        memory.setScopeKey("GLOBAL");
        memory.setMemoryKey("k");
        memory.setMemoryType(MemoryType.LEARNING);
        memory.setContent("old");
        memory.setImportance(0.5f);
        memory.setStatus(status);
        memory.setVersion(0);
        return memory;
    }
}
