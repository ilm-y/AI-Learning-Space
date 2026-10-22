package com.ails.backend.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;

/**
 * 内部服务凭据：让 Python Agent Worker 在没有用户 JWT 的情况下，以指定用户身份调用
 * 后端的<b>只读</b>检索接口。
 *
 * <h3>为什么需要它</h3>
 * 分析任务在 MQ 里排队，可能在几分钟甚至更久之后才被消费。用户 JWT 会过期，
 * 把它放进持久化消息既不可靠也不安全。Worker 因此在检索时带上：
 * <pre>
 *   X-Internal-Token:   &lt;AILS_INTERNAL_TOKEN&gt;
 *   X-Internal-User-Id: &lt;task.userId&gt;
 * </pre>
 * 本过滤器校验共享密钥后，往 SecurityContext 写入与该用户等价的 principal；
 * 后续 Controller / Service 仍走原有的 {@code SecurityUtil.currentUserId()}，
 * 不需要为 Worker 单独开一套接口，也不会绕过空间归属校验。
 *
 * <h3>边界</h3>
 * <ul>
 *   <li>只对 {@code /api/**} 生效；未配置 token 时整个过滤器是空操作（安全默认值）。</li>
 *   <li>密钥比较用常量时间，避免通过响应时间猜测。</li>
 *   <li>它只能冒充身份，不能提升权限：所有接口原本的业务校验（对话归属、空间可见性）
 *       仍然照常执行；写接口也仍然存在，因此这个 token 必须只部署在内网。</li>
 *   <li>它排在 JWT 过滤器之前；两者都只在 SecurityContext 为空时写入，因此请求同时带
 *       Bearer 与内部凭据时，最终生效的是最先执行的内部身份。两种凭据都是受信入口，
 *       不存在借此提权的路径。</li>
 * </ul>
 */
@Slf4j
@Component
public class InternalServiceAuthenticationFilter extends OncePerRequestFilter {

    public static final String TOKEN_HEADER = "X-Internal-Token";
    public static final String USER_HEADER = "X-Internal-User-Id";

    private final byte[] expectedToken;

    public InternalServiceAuthenticationFilter(@Value("${ails.internal.token:}") String configuredToken) {
        this.expectedToken = configuredToken == null
                ? new byte[0] : configuredToken.getBytes(StandardCharsets.UTF_8);
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        // 未配置凭据时彻底关闭这条入口；只处理业务 API，不碰 auth / actuator 等路径
        return expectedToken.length == 0 || !request.getRequestURI().startsWith("/api/");
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        String provided = request.getHeader(TOKEN_HEADER);
        String userIdHeader = request.getHeader(USER_HEADER);
        if (SecurityContextHolder.getContext().getAuthentication() == null
                && StringUtils.hasText(provided)
                && StringUtils.hasText(userIdHeader)
                && constantTimeEquals(provided.getBytes(StandardCharsets.UTF_8), expectedToken)) {
            try {
                Long userId = Long.valueOf(userIdHeader.trim());
                AuthUser principal = new AuthUser(userId, "agent-worker", null);
                UsernamePasswordAuthenticationToken authentication =
                        new UsernamePasswordAuthenticationToken(
                                principal, null, List.of(new SimpleGrantedAuthority("ROLE_USER")));
                SecurityContextHolder.getContext().setAuthentication(authentication);
            } catch (NumberFormatException e) {
                log.warn("内部服务凭据携带了非法的 userId：{}", userIdHeader);
            }
        }
        filterChain.doFilter(request, response);
    }

    private static boolean constantTimeEquals(byte[] left, byte[] right) {
        return MessageDigest.isEqual(left, right);
    }
}
