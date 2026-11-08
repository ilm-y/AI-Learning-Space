package com.ails.backend.service;

import com.ails.backend.common.GlobalExceptionHandler;
import com.ails.backend.config.MemorySearchConfig;
import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.controller.MemoryController;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.model.MemoryCandidateDraft;
import com.ails.backend.security.AuthUser;
import com.ails.backend.security.InternalIdentitySigner;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.SecurityAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.SecurityFilterAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.UserDetailsServiceAutoConfiguration;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;

/**
 * T14 / T15 / T21 的真实 MySQL 并发验证（默认跳过）。
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_MEMORY_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dtest=MemoryStateMachineConcurrencyIT test
 * </pre>
 *
 * <p><b>这不是 Mock 测试</b>：HTTP 层走 standalone MockMvc + 真实 {@link MemoryController}，
 * 数据层走真实 {@link MemoryService}、真实 MyBatis Mapper 与真实 MySQL 事务。
 * 所有并发断言都直接读取数据库最终状态，不以响应体或内存对象自证。</p>
 *
 * <p>使用独立库 {@code ails_memory_it}，不连接 {@code ails} 开发库。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_MEMORY_IT", matches = "true")
@SpringBootTest(
        classes = MemoryStateMachineConcurrencyIT.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql"
        })
class MemoryStateMachineConcurrencyIT {

    @Autowired
    private MemoryService memoryService;

    @Autowired
    private HybridSearchService hybridSearchService;

    @Autowired
    private JdbcTemplate jdbc;

    private MockMvc mockMvc;
    private Long userId;
    private Long spaceId;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_MEMORY_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_memory_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
    }

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders
                .standaloneSetup(new MemoryController(memoryService, hybridSearchService))
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();

        for (String table : List.of(
                "outbox_event", "evidence_source", "understanding_revision", "learning_evidence",
                "learning_understanding", "learning_state", "memory_source", "memory",
                "agent_run", "message", "conversation", "concept", "learning_space", "user")) {
            jdbc.update("DELETE FROM `" + table + "`");
        }
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "memory-it-user", "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "memory-it-user");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "memory-it-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
    }

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    // ===================== T15 并发两次确认 =====================

    @Test
    @DisplayName("T15 真实 MySQL：两个线程同时 PATCH CANDIDATE→ACTIVE，最终状态与乐观锁语义一致")
    void concurrentDoubleConfirmOnCandidateKeepsActive() throws Exception {
        Long memoryId = memoryService.createCandidate(userId, request("it/confirm",
                "待确认", MemoryStatus.CANDIDATE), MemorySourceType.MESSAGE, 1L).getId();

        List<Integer> statuses = runConcurrently(
                () -> patchStatus(memoryId, "it/confirm", "并发确认", MemoryStatus.ACTIVE),
                () -> patchStatus(memoryId, "it/confirm", "并发确认", MemoryStatus.ACTIVE));

        Map<String, Object> row = row(memoryId);
        int version = ((Number) row.get("version")).intValue();
        int successes = (int) statuses.stream().filter(status -> status == 200).count();

        // 乐观锁语义：每次成功提交都推进一个 version；冲突方得到 409 且不写库。
        assertThat(statuses).allSatisfy(status -> assertThat(status).isIn(200, 409));
        assertThat(successes).isBetween(1, 2);
        assertThat(version).isEqualTo(successes);
        assertThat(row.get("status")).isEqualTo("ACTIVE");
        assertThat(row.get("content")).isEqualTo("并发确认");
        // 409 只可能来自版本冲突；不允许出现「一边 409 一边仍是 CANDIDATE」。
        if (successes == 1) {
            assertThat(statuses).containsExactlyInAnyOrder(200, 409);
        }
    }

    // ===================== T14 合法 vs 非法并发 =====================

    @Test
    @DisplayName("T14 真实 MySQL：合法 PATCH ACTIVE 成功，非法 PATCH CANDIDATE 返回 409 且不写库")
    void illegalCandidatePatchIsRejectedWhileLegalActivePatchSucceeds() throws Exception {
        Long memoryId = memoryService.create(userId, request("it/legal-vs-illegal",
                "原始内容", MemoryStatus.ACTIVE)).id();
        int versionBefore = version(memoryId);
        LocalDateTime updatedBefore = updatedAt(memoryId);

        List<Integer> statuses = runConcurrently(
                () -> patchStatus(memoryId, "it/legal-vs-illegal", "合法更新", MemoryStatus.ACTIVE),
                () -> patchStatus(memoryId, "it/legal-vs-illegal", "非法降级", MemoryStatus.CANDIDATE));

        Map<String, Object> row = row(memoryId);

        // 合法请求必须成功；非法请求在写库前就被矩阵拒绝。
        assertThat(statuses).containsExactlyInAnyOrder(200, 409);
        assertThat(row.get("status")).isEqualTo("ACTIVE");
        assertThat(row.get("content")).isEqualTo("合法更新");
        assertThat(row.get("content")).isNotEqualTo("非法降级");
        // 非法请求没有产生 UPDATE：只推进一次版本。
        assertThat(((Number) row.get("version")).intValue()).isEqualTo(versionBefore + 1);
        assertThat(updatedAt(memoryId)).isAfterOrEqualTo(updatedBefore);
    }

    // ===================== 重复 DELETE 并发 =====================

    @Test
    @DisplayName("T13 真实 MySQL：两个线程同时 DELETE 已归档记忆，幂等且不产生任何写入")
    void concurrentDeleteOfArchivedMemoryIsIdempotent() throws Exception {
        Long memoryId = memoryService.create(userId, request("it/delete-idempotent",
                "已归档内容", MemoryStatus.ACTIVE)).id();
        memoryService.archive(userId, memoryId);

        Map<String, Object> before = row(memoryId);
        assertThat(before.get("status")).isEqualTo("ARCHIVED");
        int versionBefore = ((Number) before.get("version")).intValue();
        LocalDateTime updatedBefore = updatedAt(memoryId);

        List<Integer> statuses = runConcurrently(
                () -> deleteMemory(memoryId),
                () -> deleteMemory(memoryId));

        Map<String, Object> after = row(memoryId);

        // DELETE 对已归档记忆是短路幂等：两个请求都成功，且都不刷新 version / updated_at。
        assertThat(statuses).containsExactly(200, 200);
        assertThat(after.get("status")).isEqualTo("ARCHIVED");
        assertThat(after.get("content")).isEqualTo("已归档内容");
        assertThat(((Number) after.get("version")).intValue()).isEqualTo(versionBefore);
        assertThat(updatedAt(memoryId)).isEqualTo(updatedBefore);
    }

    // ===================== T21 Agent 更新并发 =====================

    @Test
    @DisplayName("T21 真实 MySQL：Agent UPDATE 与用户确认并发，版本冲突方放弃且不覆盖已确认内容")
    void agentUpdateRacingUserConfirmNeverOverwritesConfirmedContent() throws Exception {
        Long memoryId = memoryService.createCandidate(userId,
                new MemoryRequest(spaceId, null, MemoryType.LEARNING, "it/agent-race",
                        "初始内容", 0.5f, MemoryStatus.CANDIDATE),
                MemorySourceType.MESSAGE, 1L).getId();
        MemoryCandidateDraft draft = new MemoryCandidateDraft(
                null, spaceId, null, MemoryType.LEARNING, "it/agent-race",
                "Agent 更新", 0.6f, MemoryReconcileAction.UPDATE);

        List<Object> results = runConcurrently(
                () -> (Object) patchStatus(memoryId, "it/agent-race", "用户确认", MemoryStatus.ACTIVE),
                () -> (Object) memoryService.reconcileCandidate(
                        userId, draft, MemorySourceType.MESSAGE, 1L));

        int httpStatus = (Integer) results.get(0);
        MemoryService.ReconcileOutcome outcome = (MemoryService.ReconcileOutcome) results.get(1);
        Map<String, Object> row = row(memoryId);

        // 用户 PATCH 与 Agent UPDATE 各自 read-then-versioned-update；输的一方必须放弃。
        assertThat(httpStatus).isIn(200, 409);
        assertThat(outcome).isIn(
                MemoryService.ReconcileOutcome.UPDATED, MemoryService.ReconcileOutcome.SKIPPED);
        if (httpStatus == 200) {
            // 用户写入成功：最终必须是用户确认的状态与内容，Agent 的结果不能再覆盖。
            assertThat(row.get("status")).isEqualTo("ACTIVE");
            assertThat(row.get("content")).isEqualTo("用户确认");
            assertThat(row.get("content")).isNotEqualTo("Agent 更新");
            // Agent 先写成功时用户在其版本之上再写一次，版本为 2；否则只有用户一次写入。
            assertThat(((Number) row.get("version")).intValue())
                    .isEqualTo(outcome == MemoryService.ReconcileOutcome.UPDATED ? 2 : 1);
        } else {
            // 用户读取后 Agent 先提交，用户乐观锁冲突返回 409；Agent 只能停在 CANDIDATE。
            assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.UPDATED);
            assertThat(row.get("status")).isEqualTo("CANDIDATE");
            assertThat(row.get("content")).isEqualTo("Agent 更新");
            assertThat(((Number) row.get("version")).intValue()).isEqualTo(1);
        }
    }

    // ===================== MEM-01 更新路径身份唯一键冲突 =====================

    @Test
    @DisplayName("MEM-01 真实 MySQL：两个并发 PATCH 改成同一身份，恰好一个成功、另一个 409，且无半更新")
    void concurrentPatchToSameIdentityAllowsExactlyOne() throws Exception {
        Long first = memoryService.create(userId, spaceRequest("it/mem-a", "A")).id();
        Long second = memoryService.create(userId, spaceRequest("it/mem-b", "B")).id();

        List<PatchOutcome> outcomes = runConcurrently(
                () -> patchIdentity(first, "it/shared", "A2"),
                () -> patchIdentity(second, "it/shared", "B2"));

        // 恰好一个成功、一个因身份唯一键冲突返回 409；不允许 500。
        assertThat(outcomes).extracting(PatchOutcome::status)
                .containsExactlyInAnyOrder(200, 409);
        PatchOutcome conflict = outcomes.stream()
                .filter(outcome -> outcome.status() == 409).findFirst().orElseThrow();
        assertThat(conflict.body())
                .contains("\"code\":409")
                .contains("it/shared")
                .contains("SPACE:" + spaceId)
                .doesNotContain("\"code\":500");

        // 数据库最终只有一条目标身份，没有重复。
        Integer claimed = jdbc.queryForObject(
                "SELECT COUNT(*) FROM memory WHERE user_id = ? AND scope_key = ? AND memory_key = ?",
                Integer.class, userId, "SPACE:" + spaceId, "it/shared");
        assertThat(claimed).isEqualTo(1);

        // 输掉的一方必须保持原身份 / 原内容 / 原版本：不存在 scope 改了而 key 没改的半更新。
        Map<String, Object> firstRow = memoryRow(first);
        Map<String, Object> secondRow = memoryRow(second);
        boolean firstWon = "it/shared".equals(firstRow.get("memory_key"));
        boolean secondWon = "it/shared".equals(secondRow.get("memory_key"));
        assertThat(firstWon ^ secondWon).isTrue();

        assertThat(firstRow.get("memory_key")).isEqualTo(firstWon ? "it/shared" : "it/mem-a");
        assertThat(firstRow.get("content")).isEqualTo(firstWon ? "A2" : "A");
        assertThat(((Number) firstRow.get("version")).intValue()).isEqualTo(firstWon ? 1 : 0);
        assertThat(secondRow.get("memory_key")).isEqualTo(secondWon ? "it/shared" : "it/mem-b");
        assertThat(secondRow.get("content")).isEqualTo(secondWon ? "B2" : "B");
        assertThat(((Number) secondRow.get("version")).intValue()).isEqualTo(secondWon ? 1 : 0);

        // 无论谁赢，两行的作用域都仍然是同一个 SPACE，没有被拆成别的 scope。
        for (Map<String, Object> row : List.of(firstRow, secondRow)) {
            assertThat(row.get("scope_type")).isEqualTo("SPACE");
            assertThat(row.get("scope_key")).isEqualTo("SPACE:" + spaceId);
            assertThat(((Number) row.get("learning_space_id")).longValue()).isEqualTo(spaceId);
        }
    }

    @Test
    @DisplayName("MEM-01 真实 MySQL：单线程 PATCH 到已被占用的身份，409 且不写库")
    void singlePatchToOccupiedIdentityReturns409() throws Exception {
        authenticate();
        memoryService.create(userId, spaceRequest("it/holder", "holder"));
        Long other = memoryService.create(userId, spaceRequest("it/other", "other")).id();
        int versionBefore = version(other);

        PatchOutcome outcome = patchIdentity(other, "it/holder", "overwrite");

        assertThat(outcome.status()).isEqualTo(409);
        assertThat(outcome.body())
                .contains("\"code\":409")
                .contains("it/holder")
                .contains("SPACE:" + spaceId);
        Map<String, Object> row = memoryRow(other);
        assertThat(row.get("memory_key")).isEqualTo("it/other");
        assertThat(row.get("content")).isEqualTo("other");
        assertThat(((Number) row.get("version")).intValue()).isEqualTo(versionBefore);
    }

    @Test
    @DisplayName("MEM-01 真实 MySQL：单线程 PATCH 到空闲身份，正常 200 并落库")
    void singlePatchToFreeIdentitySucceeds() throws Exception {
        authenticate();
        Long memoryId = memoryService.create(userId, spaceRequest("it/free-before", "before")).id();

        PatchOutcome outcome = patchIdentity(memoryId, "it/free-after", "after");

        assertThat(outcome.status()).isEqualTo(200);
        Map<String, Object> row = memoryRow(memoryId);
        assertThat(row.get("memory_key")).isEqualTo("it/free-after");
        assertThat(row.get("content")).isEqualTo("after");
    }

    // ===================== helpers =====================

    /** 一次 PATCH 的 HTTP 结果；并发断言需要同时拿到状态码与响应体。 */
    private record PatchOutcome(int status, String body) {
    }

    private PatchOutcome patchIdentity(Long memoryId, String memoryKey, String content)
            throws Exception {
        String body = """
                {"learningSpaceId":%d,"memoryKey":"%s","memoryType":"LEARNING","content":"%s"}
                """.formatted(spaceId, memoryKey, content);
        MvcResult result = mockMvc.perform(patch("/api/memories/{id}", memoryId)
                .contentType(MediaType.APPLICATION_JSON)
                .content(body)).andReturn();
        return new PatchOutcome(
                result.getResponse().getStatus(), result.getResponse().getContentAsString());
    }

    private Map<String, Object> memoryRow(Long memoryId) {
        return jdbc.queryForMap("""
                SELECT memory_key, content, scope_type, scope_key,
                       learning_space_id, concept_id, status, version
                FROM memory WHERE id = ?
                """, memoryId);
    }

    private MemoryRequest spaceRequest(String memoryKey, String content) {
        return new MemoryRequest(spaceId, null, MemoryType.LEARNING, memoryKey, content, 0.6f, null);
    }

    private int patchStatus(Long memoryId, String memoryKey, String content, MemoryStatus status)
            throws Exception {
        String body = """
                {"learningSpaceId":%d,"memoryKey":"%s","memoryType":"LEARNING",
                 "content":"%s","importance":0.6,"status":"%s"}
                """.formatted(spaceId, memoryKey, content, status);
        MvcResult result = mockMvc.perform(patch("/api/memories/{id}", memoryId)
                .contentType(MediaType.APPLICATION_JSON)
                .content(body)).andReturn();
        return result.getResponse().getStatus();
    }

    private int deleteMemory(Long memoryId) throws Exception {
        MvcResult result = mockMvc.perform(delete("/api/memories/{id}", memoryId)).andReturn();
        return result.getResponse().getStatus();
    }

    private <T> List<T> runConcurrently(Callable<T> first, Callable<T> second)
            throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch ready = new CountDownLatch(2);
        CountDownLatch start = new CountDownLatch(1);
        try {
            Future<T> a = pool.submit(task(first, ready, start));
            Future<T> b = pool.submit(task(second, ready, start));
            assertThat(ready.await(5, TimeUnit.SECONDS)).isTrue();
            start.countDown();
            List<T> results = new ArrayList<>();
            results.add(a.get(30, TimeUnit.SECONDS));
            results.add(b.get(30, TimeUnit.SECONDS));
            return results;
        } finally {
            pool.shutdownNow();
        }
    }

    private <T> Callable<T> task(Callable<T> body, CountDownLatch ready, CountDownLatch start) {
        return () -> {
            ready.countDown();
            if (!start.await(5, TimeUnit.SECONDS)) {
                throw new IllegalStateException("并发测试启动超时");
            }
            SecurityContextHolder.getContext().setAuthentication(
                    new UsernamePasswordAuthenticationToken(
                            new AuthUser(userId, "memory-it-user", "token"),
                            null,
                            List.of(new SimpleGrantedAuthority("ROLE_USER"))));
            try {
                return body.call();
            } finally {
                SecurityContextHolder.clearContext();
            }
        };
    }

    /** 单线程用例直接走 MockMvc 时，也需要与并发线程相同的认证上下文。 */
    private void authenticate() {
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        new AuthUser(userId, "memory-it-user", "token"),
                        null,
                        List.of(new SimpleGrantedAuthority("ROLE_USER"))));
    }

    private Map<String, Object> row(Long memoryId) {
        return jdbc.queryForMap(
                "SELECT status, content, version, updated_at FROM memory WHERE id = ?", memoryId);
    }

    private int version(Long memoryId) {
        return ((Number) row(memoryId).get("version")).intValue();
    }

    private LocalDateTime updatedAt(Long memoryId) {
        java.sql.Timestamp value = jdbc.queryForObject(
                "SELECT updated_at FROM memory WHERE id = ?", java.sql.Timestamp.class, memoryId);
        return value == null ? null : value.toLocalDateTime();
    }

    private static MemoryRequest request(String memoryKey, String content, MemoryStatus status) {
        return new MemoryRequest(null, null, MemoryType.LEARNING, memoryKey, content, 0.5f, status);
    }

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }

    @SpringBootConfiguration
    @EnableAutoConfiguration(exclude = {
            SecurityAutoConfiguration.class,
            SecurityFilterAutoConfiguration.class,
            UserDetailsServiceAutoConfiguration.class
    })
    @Import({
            MemoryService.class,
            ConceptService.class,
            LearningSpaceService.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class,
            MemoryOutboxPublisher.class,
            HybridSearchService.class,
            MemorySearchConfig.class,
            AgentMemoryClient.class,
            InternalIdentitySigner.class
    })
    static class TestApplication {
    }
}
