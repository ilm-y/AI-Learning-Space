package com.ails.backend.security;

import com.ails.backend.utils.JwtUtil;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 完整 Spring ApplicationContext 的安全回归测试。
 *
 * <p>与只 mock service 的 {@code DocumentControllerTest} 不同，这里启动真实 Web 上下文、
 * 真实 SecurityFilterChain、真实数据库和真实 JWT 解析，专门防止
 * {@code SecurityConfig} 的过滤器注册顺序问题再次把应用启动打断。</p>
 *
 * <p>默认跳过，避免没有 MySQL 的环境里 {@code mvn test} 失败。启用方式：</p>
 * <pre>
 * AILS_SECURITY_IT=true \
 * AILS_IT_MYSQL_PASSWORD=... \
 * ./mvnw -Dtest=SecurityContextIntegrationTest test
 * </pre>
 *
 * <p>使用独立数据库 {@code ails_security_it}，不连接 {@code ails} 开发库。
 * 每次运行使用随机用户名，避免重复执行时与历史数据冲突。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_SECURITY_IT", matches = "true")
@SpringBootTest(properties = {
        "ails.agent.gateway=stub",
        "ails.analysis.mode=inline",
        "ails.internal.token=" + SecurityContextIntegrationTest.INTERNAL_TOKEN
})
@AutoConfigureMockMvc
class SecurityContextIntegrationTest {

    static final String INTERNAL_TOKEN = "security-it-internal-token";

    @Autowired
    private ApplicationContext applicationContext;

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private JwtUtil jwtUtil;

    private Long userId;
    private Long otherUserId;
    private Long spaceId;
    private Long otherSpaceId;
    private String username;
    private String token;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_SECURITY_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_security_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", "root"));
    }

    @BeforeEach
    void setUp() {
        String suffix = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        username = "security_it_" + suffix;
        userId = insertUser(username);
        otherUserId = insertUser("security_it_other_" + suffix);
        spaceId = insertSpace(userId, "security-it-space");
        otherSpaceId = insertSpace(otherUserId, "security-it-other-space");
        token = jwtUtil.generateToken(userId, username);
    }

    @Test
    void contextLoadsWithSecurityFilterChain() {
        assertThat(applicationContext.getBean(SecurityFilterChain.class)).isNotNull();
    }

    @Test
    void unauthenticatedRequestToProtectedEndpointReturns401Json() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents", spaceId))
                .andExpect(status().isUnauthorized())
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andExpect(jsonPath("$.code").value(401))
                .andExpect(jsonPath("$.message").value("未登录或 Token 已过期"));
    }

    @Test
    void jwtAuthenticatedUserCanAccessOwnProfile() throws Exception {
        mockMvc.perform(get("/api/user/me")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.id").value(userId))
                .andExpect(jsonPath("$.data.username").value(username));
    }

    @Test
    void jwtAuthenticatedUserKeepsDocumentSpacePermissionChecks() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents", spaceId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data").isArray())
                .andExpect(jsonPath("$.data").isEmpty());

        mockMvc.perform(get("/api/spaces/{spaceId}/documents", otherSpaceId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404))
                .andExpect(jsonPath("$.message").value("学习空间不存在"));
    }

    @Test
    void readyIdsEndpointUsesJwtIdentityAndKeepsOwnershipCheck() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents/ready-ids", spaceId)
                        .param("embeddingModel", "text-embedding-v4")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data").isArray())
                .andExpect(jsonPath("$.data").isEmpty());

        mockMvc.perform(get("/api/spaces/{spaceId}/documents/ready-ids", otherSpaceId)
                        .param("embeddingModel", "text-embedding-v4")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404));
    }

    @Test
    void unauthenticatedReadyIdsRequestReturns401() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents/ready-ids", spaceId))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value(401));
    }

    @Test
    void internalServiceTokenActsAsUserWithoutBypassingSpaceOwnership() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents", otherSpaceId)
                        .header(InternalServiceAuthenticationFilter.TOKEN_HEADER, INTERNAL_TOKEN)
                        .header(InternalServiceAuthenticationFilter.USER_HEADER, otherUserId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data").isArray());

        // 内部凭据也只能“成为”指定用户，不能绕过原有的空间归属校验。
        mockMvc.perform(get("/api/spaces/{spaceId}/documents", spaceId)
                        .header(InternalServiceAuthenticationFilter.TOKEN_HEADER, INTERNAL_TOKEN)
                        .header(InternalServiceAuthenticationFilter.USER_HEADER, otherUserId))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404));
    }

    @Test
    void internalServiceTokenCanReadReadyIdsOnlyForItsUser() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents/ready-ids", otherSpaceId)
                        .param("embeddingModel", "text-embedding-v4")
                        .header(InternalServiceAuthenticationFilter.TOKEN_HEADER, INTERNAL_TOKEN)
                        .header(InternalServiceAuthenticationFilter.USER_HEADER, otherUserId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data").isArray());

        mockMvc.perform(get("/api/spaces/{spaceId}/documents/ready-ids", spaceId)
                        .param("embeddingModel", "text-embedding-v4")
                        .header(InternalServiceAuthenticationFilter.TOKEN_HEADER, INTERNAL_TOKEN)
                        .header(InternalServiceAuthenticationFilter.USER_HEADER, otherUserId))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404));
    }

    @Test
    void internalFilterStillRunsBeforeJwtFilter() throws Exception {
        // 现有语义：内部服务凭据先建立身份，JWT 过滤器随后看到 SecurityContext 非空便不再覆盖。
        mockMvc.perform(get("/api/user/me")
                        .header("Authorization", "Bearer " + token)
                        .header(InternalServiceAuthenticationFilter.TOKEN_HEADER, INTERNAL_TOKEN)
                        .header(InternalServiceAuthenticationFilter.USER_HEADER, otherUserId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(otherUserId));
    }

    @Test
    void invalidInternalTokenIsRejected() throws Exception {
        mockMvc.perform(get("/api/spaces/{spaceId}/documents", otherSpaceId)
                        .header(InternalServiceAuthenticationFilter.TOKEN_HEADER, "wrong-token")
                        .header(InternalServiceAuthenticationFilter.USER_HEADER, otherUserId))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value(401));
    }

    @Test
    void publicAuthEndpointRemainsReachableWithoutAuthentication() throws Exception {
        // 请求能进入 Controller 并触发参数校验（400），而不是被安全链拦成 401。
        mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    // ===================== helpers =====================

    private Long insertUser(String name) {
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", name, "x");
        return jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, name);
    }

    private Long insertSpace(Long ownerId, String name) {
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                ownerId, name);
        return jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }
}
