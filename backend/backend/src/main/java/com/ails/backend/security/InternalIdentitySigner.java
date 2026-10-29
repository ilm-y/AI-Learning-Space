package com.ails.backend.security;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.util.Base64;
import java.util.Optional;
import java.util.UUID;

/**
 * 内部用户身份声明：签发与校验。
 *
 * <p>共享 token 只能证明“调用方是内部服务”，不能证明“代表哪个用户”。本类用
 * HMAC-SHA256 对以下声明签名：</p>
 *
 * <pre>
 *   iss  签发方（固定 learning-backend）
 *   sub  userId
 *   aud  接收方
 *   iat  签发时间（秒）
 *   exp  过期时间（秒）
 *   jti  关联 id（不是重放防护，见下）
 *   scope 可选；后端委托声明固定为 analysis:read
 * </pre>
 *
 * <h3>为什么有两把密钥</h3>
 * <ul>
 *   <li>{@code identity-secret}：Spring → Agent 的“代表用户”声明（aud=learning-agent）。
 *       Agent 必须持有同一把密钥才能校验，因此 Agent 也能签发同 audience 的声明；
 *       这只会影响 Agent 自己的入口，后端不接受该 audience。</li>
 *   <li>{@code delegation-secret}：Spring → 后端读接口的“分析委托”声明
 *       （aud=learning-backend，scope=analysis:read）。<b>这把密钥只存在于 Spring 进程</b>，
 *       Agent 不持有；Agent 只能转发 Spring 放在 MQ 任务里的声明，不能自行签发后端接受的
 *       任意用户身份。</li>
 * </ul>
 *
 * <h3>jti 的语义</h3>
 * 声明有效期内可以多次使用，{@code jti} 只用于日志/审计关联，<b>不提供重放检测</b>。
 * 后端接受声明只对 GET 读接口生效，有效期由配置限定；泄露的声明在过期前仍可重放，
 * 这是当前明确接受的残余风险，不能对外宣称“一次性令牌”。
 */
@Component
public class InternalIdentitySigner {

    static final String VERSION = "v1";
    static final String ISSUER = "learning-backend";
    static final String DELEGATION_SCOPE = "analysis:read";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final byte[] identitySecret;
    private final byte[] delegationSecret;
    private final String audience;
    private final long ttlSeconds;
    private final String backendAudience;
    private final long maxTtlSeconds;
    private final long delegationTtlSeconds;
    private final long delegationMaxTtlSeconds;
    private final long clockSkewSeconds;
    private final Clock clock;

    @Autowired
    public InternalIdentitySigner(
            @Value("${ails.internal.identity-secret:}") String identitySecret,
            @Value("${ails.internal.delegation-secret:}") String delegationSecret,
            @Value("${ails.internal.identity-audience:learning-agent}") String audience,
            @Value("${ails.internal.identity-ttl-seconds:60}") long ttlSeconds,
            @Value("${ails.internal.backend-identity-audience:learning-backend}") String backendAudience,
            @Value("${ails.internal.identity-max-ttl-seconds:300}") long maxTtlSeconds,
            @Value("${ails.internal.identity-clock-skew-seconds:5}") long clockSkewSeconds,
            @Value("${ails.internal.delegation-ttl-seconds:900}") long delegationTtlSeconds,
            @Value("${ails.internal.delegation-max-ttl-seconds:3600}") long delegationMaxTtlSeconds) {
        this(identitySecret, delegationSecret, audience, ttlSeconds, backendAudience,
                maxTtlSeconds, clockSkewSeconds, delegationTtlSeconds,
                delegationMaxTtlSeconds, Clock.systemUTC());
    }

    /** 测试用：允许固定时钟，验证过期/有效期上界，不靠 sleep。 */
    InternalIdentitySigner(String identitySecret, String delegationSecret, String audience,
                           long ttlSeconds, String backendAudience, long maxTtlSeconds,
                           long clockSkewSeconds, long delegationTtlSeconds,
                           long delegationMaxTtlSeconds, Clock clock) {
        this.identitySecret = bytes(identitySecret);
        this.delegationSecret = bytes(delegationSecret);
        this.audience = blankToDefault(audience, "learning-agent");
        this.ttlSeconds = ttlSeconds <= 0 ? 60 : ttlSeconds;
        this.backendAudience = blankToDefault(backendAudience, "learning-backend");
        this.maxTtlSeconds = maxTtlSeconds <= 0 ? 300 : maxTtlSeconds;
        this.clockSkewSeconds = Math.max(0, clockSkewSeconds);
        this.delegationTtlSeconds = delegationTtlSeconds <= 0 ? 900 : delegationTtlSeconds;
        this.delegationMaxTtlSeconds = delegationMaxTtlSeconds <= 0
                ? 3600 : delegationMaxTtlSeconds;
        this.clock = clock == null ? Clock.systemUTC() : clock;
    }

    public boolean isConfigured() {
        return identitySecret.length > 0;
    }

    public boolean isDelegationConfigured() {
        return delegationSecret.length > 0;
    }

    /**
     * 签发 Spring → Agent 的声明（aud=learning-agent）。未配置密钥时抛
     * {@link IllegalStateException}：调用方必须把它当作配置失败，不能降级成只发共享 token。
     */
    public String sign(Long userId) {
        return signForAudience(userId, audience);
    }

    /** 指定接收方签发；只使用 Agent 侧共享的 identity 密钥。 */
    public String signForAudience(Long userId, String targetAudience) {
        return signToken(userId, targetAudience, identitySecret, ttlSeconds, null, ISSUER,
                "未配置 ails.internal.identity-secret，无法签发内部用户身份");
    }

    /**
     * 签发 Spring → 后端读接口的分析委托声明。
     * <p>只有 Spring 持有 {@code delegation-secret}；Agent 拿不到这把密钥，因此不能
     * 自行签发后端接受的身份。MQ 任务把本方法生成的声明放进消息，Agent 原样转发。</p>
     */
    public String signDelegation(Long userId) {
        return signToken(userId, backendAudience, delegationSecret,
                delegationTtlSeconds, DELEGATION_SCOPE, ISSUER,
                "未配置 ails.internal.delegation-secret，无法签发后端委托身份");
    }

    /**
     * 包内可见的测试入口：用 delegation 密钥签发任意 audience/issuer/scope/有效期，
     * 用于验证校验器确实拒绝错误声明。生产代码不调用。
     */
    String signDelegationForTest(Long userId, String targetAudience, String issuer,
                                 String scope, long ttl) {
        return signToken(userId, targetAudience, delegationSecret, ttl, scope, issuer,
                "未配置 ails.internal.delegation-secret，无法签发后端委托身份");
    }

    /**
     * 校验 Agent 回访后端时携带的委托声明。返回可信 userId 与权限范围。
     * 任何格式、签名、issuer、接收方、scope 或有效期问题都返回 empty，
     * 调用方不得降级信任普通 Header。
     */
    public Optional<DelegationClaims> verifyDelegation(String token) {
        return verify(token, delegationSecret, backendAudience, DELEGATION_SCOPE,
                delegationMaxTtlSeconds);
    }

    /** 兼容旧调用点：只返回 userId。 */
    public Optional<Long> verifyForBackend(String token) {
        return verifyDelegation(token).map(DelegationClaims::userId);
    }

    /** 校验 Spring → Agent 的声明；只信任 identity 密钥与指定 audience。 */
    public Optional<Long> verifyForAudience(String token, String expectedAudience) {
        return verify(token, identitySecret, expectedAudience, null, maxTtlSeconds)
                .map(DelegationClaims::userId);
    }

    public record DelegationClaims(Long userId, String scope) {
    }

    private String signToken(Long userId, String targetAudience, byte[] secret, long ttl,
                             String scope, String issuer, String missingSecretMessage) {
        if (userId == null || userId <= 0) {
            throw new IllegalArgumentException("userId 必须是正整数");
        }
        if (secret.length == 0) {
            throw new IllegalStateException(missingSecretMessage);
        }
        if (targetAudience == null || targetAudience.isBlank()) {
            throw new IllegalArgumentException("内部身份接收方不能为空");
        }
        long issuedAt = Instant.now(clock).getEpochSecond();
        ObjectNode payload = MAPPER.createObjectNode();
        payload.put("iss", issuer == null || issuer.isBlank() ? ISSUER : issuer);
        payload.put("sub", userId);
        payload.put("aud", targetAudience);
        payload.put("iat", issuedAt);
        payload.put("exp", issuedAt + ttl);
        payload.put("jti", UUID.randomUUID().toString());
        if (scope != null) {
            payload.put("scope", scope);
        }
        try {
            String encoded = Base64.getUrlEncoder().withoutPadding()
                    .encodeToString(MAPPER.writeValueAsBytes(payload));
            String signingInput = VERSION + "." + encoded;
            return signingInput + "." + sign(signingInput, secret);
        } catch (Exception e) {
            throw new IllegalStateException("签发内部身份声明失败", e);
        }
    }

    private Optional<DelegationClaims> verify(String token, byte[] secret,
                                              String expectedAudience, String expectedScope,
                                              long maxAllowedTtl) {
        if (secret.length == 0 || token == null || token.isBlank()
                || expectedAudience == null || expectedAudience.isBlank()) {
            return Optional.empty();
        }
        String[] parts = token.split("\\.");
        if (parts.length != 3 || !VERSION.equals(parts[0])) {
            return Optional.empty();
        }
        try {
            String expected = sign(parts[0] + "." + parts[1], secret);
            if (!MessageDigest.isEqual(
                    expected.getBytes(StandardCharsets.US_ASCII),
                    parts[2].getBytes(StandardCharsets.US_ASCII))) {
                return Optional.empty();
            }
            JsonNode payload = MAPPER.readTree(
                    Base64.getUrlDecoder().decode(parts[1]));
            if (payload == null || !payload.isObject()) {
                return Optional.empty();
            }
            if (!ISSUER.equals(payload.path("iss").asText(""))) {
                return Optional.empty();
            }
            JsonNode subject = payload.get("sub");
            if (subject == null || !subject.canConvertToLong() || subject.asLong() <= 0) {
                return Optional.empty();
            }
            if (!expectedAudience.equals(payload.path("aud").asText(null))) {
                return Optional.empty();
            }
            JsonNode issued = payload.get("iat");
            JsonNode expires = payload.get("exp");
            if (issued == null || expires == null
                    || !issued.canConvertToLong() || !expires.canConvertToLong()) {
                return Optional.empty();
            }
            long now = Instant.now(clock).getEpochSecond();
            long iat = issued.asLong();
            long exp = expires.asLong();
            if (exp <= now - clockSkewSeconds || iat > now + clockSkewSeconds) {
                return Optional.empty();
            }
            if (exp <= iat || exp - iat > maxAllowedTtl) {
                return Optional.empty();
            }
            if (payload.path("jti").asText("").isBlank()) {
                return Optional.empty();
            }
            String scope = payload.path("scope").asText("");
            if (expectedScope != null && !expectedScope.equals(scope)) {
                return Optional.empty();
            }
            return Optional.of(new DelegationClaims(subject.asLong(), scope));
        } catch (Exception e) {
            return Optional.empty();
        }
    }

    private String sign(String signingInput, byte[] secret) throws GeneralSecurityException {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret, "HmacSHA256"));
        byte[] digest = mac.doFinal(signingInput.getBytes(StandardCharsets.US_ASCII));
        return Base64.getUrlEncoder().withoutPadding().encodeToString(digest);
    }

    private static byte[] bytes(String value) {
        return value == null ? new byte[0] : value.getBytes(StandardCharsets.UTF_8);
    }

    private static String blankToDefault(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }
}
