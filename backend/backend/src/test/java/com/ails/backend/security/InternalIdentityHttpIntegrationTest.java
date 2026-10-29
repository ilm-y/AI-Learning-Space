package com.ails.backend.security;

import com.ails.backend.utils.JwtUtil;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 真实 HTTP 端口下的内部身份边界测试。
 *
 * <p>与 {@link SecurityContextIntegrationTest}（MockMvc，不经过真实网络栈）互补：
 * 这里用随机端口启动真实的 Tomcat，用 {@link TestRestTemplate} 发请求，验证
 * 过滤器链、Header 解析和拒绝行为在真实 HTTP 请求上一致。</p>
 *
 * <p>默认跳过；设置 {@code AILS_SECURITY_IT=true} 并提供 MySQL 后执行：</p>
 * <pre>
 * AILS_SECURITY_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dtest=InternalIdentityHttpIntegrationTest test
 * </pre>
 */
@EnabledIfEnvironmentVariable(named = "AILS_SECURITY_IT", matches = "true")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "ails.agent.gateway=stub",
        "ails.analysis.mode=inline",
        "ails.internal.token=" + InternalIdentityHttpIntegrationTest.INTERNAL_TOKEN,
        "ails.internal.identity-secret=" + InternalIdentityHttpIntegrationTest.IDENTITY_SECRET,
        "ails.internal.delegation-secret=" + InternalIdentityHttpIntegrationTest.DELEGATION_SECRET
})
class InternalIdentityHttpIntegrationTest {

    static final String INTERNAL_TOKEN = "http-it-internal-token";
    static final String IDENTITY_SECRET = "http-it-identity-secret";
    static final String DELEGATION_SECRET = "http-it-delegation-secret";

    @Autowired
    private TestRestTemplate rest;

    @Autowired
    private InternalIdentitySigner identitySigner;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private JwtUtil jwtUtil;

    private Long userId;
    private Long spaceId;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_SECURITY_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_security_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
    }

    @BeforeEach
    void setUp() {
        String suffix = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        String username = "http_it_" + suffix;
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", username, "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, username);
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "http-it-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
    }

    @Test
    @DisplayName("真实 HTTP：无凭据访问受保护接口返回 401")
    void noCredentialsReturns401OverHttp() {
        ResponseEntity<String> response = rest.getForEntity(
                "/api/spaces/{spaceId}/documents", String.class, spaceId);
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
    }

    @Test
    @DisplayName("真实 HTTP：Spring 签发的委托声明可代表指定用户读取")
    void validDelegationReadsAsUserOverHttp() {
        ResponseEntity<String> response = rest.exchange(
                "/api/spaces/{spaceId}/documents", HttpMethod.GET,
                new HttpEntity<>(internalHeaders(identitySigner.signDelegation(userId))),
                String.class, spaceId);
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getBody()).contains("\"code\":0");
    }

    @Test
    @DisplayName("真实 HTTP：Agent 用 identity-secret 伪造的后端身份被拒绝")
    void agentSignedBackendIdentityIsRejectedOverHttp() {
        String forged = identitySigner.signForAudience(userId, "learning-backend");
        ResponseEntity<String> response = rest.exchange(
                "/api/spaces/{spaceId}/documents", HttpMethod.GET,
                new HttpEntity<>(internalHeaders(forged)), String.class, spaceId);
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
    }

    @Test
    @DisplayName("真实 HTTP：篡改签名、错误 audience/issuer 与过期声明都被拒绝")
    void invalidClaimsAreRejectedOverHttp() {
        String valid = identitySigner.signDelegation(userId);
        String tampered = valid.substring(0, valid.length() - 1)
                + (valid.endsWith("A") ? "B" : "A");

        assertThat(getWithIdentity(tampered).getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(getWithIdentity(identitySigner.signDelegationForTest(userId,
                "learning-agent", InternalIdentitySigner.ISSUER,
                InternalIdentitySigner.DELEGATION_SCOPE, 900)).getStatusCode())
                .isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(getWithIdentity(identitySigner.signDelegationForTest(userId,
                "learning-backend", "attacker-issuer",
                InternalIdentitySigner.DELEGATION_SCOPE, 900)).getStatusCode())
                .isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(getWithIdentity(identitySigner.signDelegationForTest(userId,
                "learning-backend", InternalIdentitySigner.ISSUER,
                InternalIdentitySigner.DELEGATION_SCOPE, -60)).getStatusCode())
                .isEqualTo(HttpStatus.UNAUTHORIZED);
    }

    @Test
    @DisplayName("真实 HTTP：客户端 body userId 不能覆盖 JWT 身份")
    void bodyUserIdCannotOverrideJwtIdentityOverHttp() {
        String username = jdbc.queryForObject(
                "SELECT username FROM user WHERE id = ?", String.class, userId);
        String token = jwtUtil.generateToken(userId, username);
        HttpHeaders headers = new HttpHeaders();
        headers.setBearerAuth(token);
        headers.setContentType(org.springframework.http.MediaType.APPLICATION_JSON);
        String body = "{\"name\":\"http-forged-space\",\"visibility\":\"PRIVATE\",\"userId\":999999}";

        ResponseEntity<String> response = rest.exchange(
                "/api/spaces", HttpMethod.POST, new HttpEntity<>(body, headers), String.class);
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getBody()).contains("\"userId\":" + userId);

        Long createdId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE name = 'http-forged-space' "
                        + "ORDER BY id DESC LIMIT 1", Long.class);
        assertThat(jdbc.queryForObject(
                "SELECT user_id FROM learning_space WHERE id = ?", Long.class, createdId))
                .isEqualTo(userId);
    }

    private ResponseEntity<String> getWithIdentity(String identity) {
        return rest.exchange("/api/spaces/{spaceId}/documents", HttpMethod.GET,
                new HttpEntity<>(internalHeaders(identity)), String.class, spaceId);
    }

    private HttpHeaders internalHeaders(String identity) {
        HttpHeaders headers = new HttpHeaders();
        headers.set(InternalServiceAuthenticationFilter.TOKEN_HEADER, INTERNAL_TOKEN);
        headers.set(InternalServiceAuthenticationFilter.IDENTITY_HEADER, identity);
        headers.set(InternalServiceAuthenticationFilter.USER_HEADER, String.valueOf(userId));
        return headers;
    }

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }
}
