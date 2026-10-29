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
import java.util.Optional;

/**
 * 内部服务委托凭据：让 Python Agent Worker 在没有用户 JWT 的情况下，以指定用户身份
 * 调用后端的<b>只读</b>检索接口。
 *
 * <h3>信任链</h3>
 * <pre>
 *   Spring Boot 在创建 MQ 分析任务时，用 delegation-secret 签发：
 *     X-Internal-Identity: v1.&lt;payload&gt;.&lt;hmac&gt;   （aud=learning-backend,
 *                                                        scope=analysis:read）
 *   Agent Worker 只把这条声明原样转发，不重新签发、不修改 sub。
 *   本过滤器用同一把 delegation-secret 校验，用户身份只来自声明里的 sub。
 * </pre>
 *
 * <p>delegation-secret 只存在于 Spring 进程；Agent 即使拿到共享 token，也没有签发
 * 后端可接受声明的材料。共享 token 仍必须正确，但它本身不再构成用户身份。</p>
 *
 * <h3>边界</h3>
 * <ul>
 *   <li>只对 {@code /api/**} 的 <b>GET</b> 生效。内部凭据不能写业务数据，
 *       因此即使声明被重放，也不能创建/修改学习记录。</li>
 *   <li>只接受 scope=analysis:read 的声明；audience 必须是 learning-backend。</li>
 *   <li>声明有效期内可多次使用，{@code jti} 只做审计关联，<b>不做重放检测</b>。
 *       这是当前明确接受的短期风险。</li>
 *   <li>未配置 token 时整个过滤器是空操作（安全默认值）。</li>
 *   <li>密钥比较用常量时间，避免通过响应时间猜测。</li>
 * </ul>
 */
@Slf4j
@Component
public class InternalServiceAuthenticationFilter extends OncePerRequestFilter {

    public static final String TOKEN_HEADER = "X-Internal-Token";
    public static final String USER_HEADER = "X-Internal-User-Id";
    public static final String IDENTITY_HEADER = "X-Internal-Identity";

    private final byte[] expectedToken;
    private final InternalIdentitySigner identitySigner;

    public InternalServiceAuthenticationFilter(
            @Value("${ails.internal.token:}") String configuredToken,
            InternalIdentitySigner identitySigner) {
        this.expectedToken = configuredToken == null
                ? new byte[0] : configuredToken.getBytes(StandardCharsets.UTF_8);
        this.identitySigner = identitySigner;
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
        String identity = request.getHeader(IDENTITY_HEADER);
        // 内部凭据只读。POST/PUT/PATCH/DELETE 一律不建立内部身份；若同时带合法 JWT，
        // 仍由 JWT 过滤器按用户自己的权限处理，不会被这条分支放大。
        boolean readOnly = "GET".equalsIgnoreCase(request.getMethod());
        if (readOnly
                && SecurityContextHolder.getContext().getAuthentication() == null
                && StringUtils.hasText(provided)
                && constantTimeEquals(provided.getBytes(StandardCharsets.UTF_8), expectedToken)) {
            Optional<InternalIdentitySigner.DelegationClaims> verified =
                    identitySigner.verifyDelegation(identity);
            if (verified.isEmpty()) {
                log.warn("内部服务请求缺少或携带了无效的委托声明，拒绝建立用户上下文");
            } else if (StringUtils.hasText(userIdHeader)
                    && !userIdHeader.trim().equals(String.valueOf(verified.get().userId()))) {
                log.warn("内部委托声明与 X-Internal-User-Id 不一致，拒绝建立用户上下文");
            } else {
                AuthUser principal = new AuthUser(verified.get().userId(), "agent-worker", null);
                UsernamePasswordAuthenticationToken authentication =
                        new UsernamePasswordAuthenticationToken(
                                principal, null, List.of(new SimpleGrantedAuthority("ROLE_USER")));
                SecurityContextHolder.getContext().setAuthentication(authentication);
            }
        }
        filterChain.doFilter(request, response);
    }

    private static boolean constantTimeEquals(byte[] left, byte[] right) {
        return MessageDigest.isEqual(left, right);
    }
}
