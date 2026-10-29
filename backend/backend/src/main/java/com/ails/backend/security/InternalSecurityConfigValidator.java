package com.ails.backend.security;

import jakarta.annotation.PostConstruct;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * 内部凭据配置的启动期校验。
 *
 * <p>内部入口一旦启用（{@code ails.internal.token} 非空），以下配置错误会让启动直接失败，
 * 而不是带着“看起来能跑、其实签名/验证用同一把密钥”的状态上线：</p>
 *
 * <ul>
 *   <li>identity-secret 或 delegation-secret 缺失；</li>
 *   <li>共享 token 与任一签名密钥相同；</li>
 *   <li>两把签名密钥相同（等于 Agent 仍能签发后端接受的声明）；</li>
 *   <li>Agent audience 与后端 audience 相同。</li>
 * </ul>
 *
 * <p>内部入口未启用时（默认空 token）不做限制，应用可以只跑普通 JWT 链路。</p>
 */
@Component
public class InternalSecurityConfigValidator {

    private final String token;
    private final String identitySecret;
    private final String delegationSecret;
    private final String identityAudience;
    private final String backendAudience;

    public InternalSecurityConfigValidator(
            @Value("${ails.internal.token:}") String token,
            @Value("${ails.internal.identity-secret:}") String identitySecret,
            @Value("${ails.internal.delegation-secret:}") String delegationSecret,
            @Value("${ails.internal.identity-audience:learning-agent}") String identityAudience,
            @Value("${ails.internal.backend-identity-audience:learning-backend}") String backendAudience) {
        this.token = trim(token);
        this.identitySecret = trim(identitySecret);
        this.delegationSecret = trim(delegationSecret);
        this.identityAudience = trim(identityAudience);
        this.backendAudience = trim(backendAudience);
    }

    @PostConstruct
    void validate() {
        if (token.isEmpty()) {
            return;
        }
        if (identitySecret.isEmpty() || delegationSecret.isEmpty()) {
            throw new IllegalStateException(
                    "内部入口已启用，但缺少 ails.internal.identity-secret 或 "
                            + "ails.internal.delegation-secret；拒绝以不完整配置启动");
        }
        if (same(identitySecret, delegationSecret)) {
            throw new IllegalStateException(
                    "ails.internal.identity-secret 与 ails.internal.delegation-secret 不能相同："
                            + "Agent 持有前者，相同会让 Agent 可以签发后端接受的用户身份");
        }
        if (same(token, identitySecret) || same(token, delegationSecret)) {
            throw new IllegalStateException(
                    "ails.internal.token 不能与任何签名密钥相同");
        }
        if (identityAudience.equals(backendAudience)) {
            throw new IllegalStateException(
                    "内部身份的两个 audience 不能相同");
        }
    }

    private static boolean same(String left, String right) {
        return MessageDigest.isEqual(
                left.getBytes(StandardCharsets.UTF_8),
                right.getBytes(StandardCharsets.UTF_8));
    }

    private static String trim(String value) {
        return value == null ? "" : value.trim();
    }
}
