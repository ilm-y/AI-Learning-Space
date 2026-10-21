package com.ails.backend.security;

import com.ails.backend.utils.JwtUtil;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.authentication.WebAuthenticationDetailsSource;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.List;

/**
 * JWT 认证过滤器：解析 Authorization: Bearer &lt;token&gt;，校验通过则写入 SecurityContext。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class JwtAuthenticationFilter extends OncePerRequestFilter {

    private final JwtUtil jwtUtil;

    /**
     * ASYNC 分发也要重新认证。
     * <p>
     * {@link OncePerRequestFilter} 默认跳过 ASYNC 分发，但 SSE 流结束（{@code SseEmitter#complete()}）
     * 时容器会对同一个请求再做一次 ASYNC 分发，重新走一遍过滤器链。跳过本过滤器意味着那一次
     * 分发里 SecurityContext 为空，{@code anyRequest().authenticated()} 直接抛
     * {@code AccessDeniedException}；此时响应早已提交（Content-Type 已是 text/event-stream），
     * Spring Security 的异常处理写不进去，只能把连接硬关 —— 客户端看到的就是
     * 「chunked 响应缺少终止块」（httpx: RemoteProtocolError，curl: exit 18），
     * 即使它已经收到了完整的 message.end。
     * <p>
     * 重新解析 {@code Authorization} 头即可：请求头在同一个请求的所有分发之间是保留的。
     */
    @Override
    protected boolean shouldNotFilterAsyncDispatch() {
        return false;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        String token = resolveToken(request);
        if (token != null && SecurityContextHolder.getContext().getAuthentication() == null) {
            try {
                Long userId = jwtUtil.getUserId(token);
                String username = jwtUtil.getUsername(token);
                if (userId != null) {
                    AuthUser principal = new AuthUser(userId, username, token);
                    UsernamePasswordAuthenticationToken authentication =
                            new UsernamePasswordAuthenticationToken(
                                    principal, null, List.of(new SimpleGrantedAuthority("ROLE_USER")));
                    authentication.setDetails(new WebAuthenticationDetailsSource().buildDetails(request));
                    SecurityContextHolder.getContext().setAuthentication(authentication);
                }
            } catch (Exception e) {
                // token 非法/过期：不设置认证，交给 entryPoint 返回 401
                log.debug("JWT 校验失败: {}", e.getMessage());
            }
        }
        filterChain.doFilter(request, response);
    }

    private String resolveToken(HttpServletRequest request) {
        String header = request.getHeader("Authorization");
        if (header != null && header.startsWith("Bearer ")) {
            return header.substring(7);
        }
        return null;
    }
}
