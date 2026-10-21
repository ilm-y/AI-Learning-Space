package com.ails.backend.security;

import com.ails.backend.utils.JwtUtil;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.FilterChain;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.core.context.SecurityContextHolder;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * JWT 过滤器在 ASYNC 分发上的行为。
 * <p>
 * 这条用例防的是 BC-003：SSE 结束时容器会做一次 ASYNC 分发，
 * {@link org.springframework.web.filter.OncePerRequestFilter} 默认跳过它，
 * 于是那一轮请求没有认证信息，安全链把已经提交的流硬关掉。
 */
class JwtAuthenticationFilterTest {

    @AfterEach
    void clearContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    @DisplayName("ASYNC 分发仍会解析 Authorization 并写入 SecurityContext")
    void asyncDispatchStillAuthenticates() throws Exception {
        JwtUtil jwtUtil = mock(JwtUtil.class);
        when(jwtUtil.getUserId("token")).thenReturn(7L);
        when(jwtUtil.getUsername("token")).thenReturn("alice");

        MockHttpServletRequest request = new MockHttpServletRequest();
        request.setDispatcherType(DispatcherType.ASYNC);
        request.addHeader("Authorization", "Bearer token");
        FilterChain chain = mock(FilterChain.class);

        new JwtAuthenticationFilter(jwtUtil).doFilter(request, new MockHttpServletResponse(), chain);

        assertThat(SecurityContextHolder.getContext().getAuthentication()).isNotNull();
        assertThat(SecurityContextHolder.getContext().getAuthentication().getPrincipal())
                .isEqualTo(new AuthUser(7L, "alice", "token"));
    }
}
