package com.ails.backend.service;

import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
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
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * FULLTEXT + ngram 的真实 MySQL 验证（默认跳过）。
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_MEMORY_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dtest=MemoryFulltextSearchIT test
 * </pre>
 *
 * <p><b>这不是 Mock 测试</b>：走真实 {@link MemoryService}、真实 MyBatis Mapper、
 * 真实 MySQL FULLTEXT 索引。断言直接来自数据库查询结果，不用内存对象自证。</p>
 *
 * <p>使用独立库 {@code ails_memory_it}（与 {@code MemoryStateMachineConcurrencyIT} 相同），
 * 不连接 {@code ails} 开发库。</p>
 *
 * <p><b>索引自愈：</b>测试库可能在本改造之前就已存在，而
 * {@code CREATE TABLE IF NOT EXISTS} 不会给老表补 FULLTEXT 索引。因此
 * {@link #ensureFulltextIndex()} 会先查 {@code information_schema}，缺失时补建 ——
 * 这同时验证了迁移脚本里那段 DDL 在真实 MySQL 上是可执行的。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_MEMORY_IT", matches = "true")
@SpringBootTest(
        classes = MemoryFulltextSearchIT.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql"
        })
class MemoryFulltextSearchIT {

    @Autowired
    private MemoryService memoryService;

    @Autowired
    private MemoryMapper memoryMapper;

    @Autowired
    private JdbcTemplate jdbc;

    private Long userId;
    private Long spaceId;
    private Long otherSpaceId;
    private Long conceptId;

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
        for (String table : List.of(
                "outbox_event", "evidence_source", "understanding_revision", "learning_evidence",
                "learning_understanding", "learning_state", "memory_source", "memory",
                "agent_run", "message", "conversation", "test_task", "concept_relation",
                "concept", "learning_space", "user")) {
            jdbc.update("DELETE FROM `" + table + "`");
        }
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "fulltext-it-user", "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "fulltext-it-user");

        spaceId = insertSpace(userId, "fulltext-space-a");
        otherSpaceId = insertSpace(userId, "fulltext-space-b");
        conceptId = insertConcept(spaceId, "Recursion");

        ensureFulltextIndex();
    }

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    // ===================== 基础召回 =====================

    @Test
    @DisplayName("F1 中文关键词：MATCH ... AGAINST 能召回包含该词的 ACTIVE 记忆")
    void chineseKeywordFindsActiveMemory() {
        Long memoryId = memoryService.create(userId,
                request(null, null, "ft/cn", "用户偏好用递归解释分治", 0.5f)).id();
        memoryService.create(userId, request(null, null, "ft/other", "喜欢看纪录片", 0.5f));

        List<MemoryResponse> hits = memoryService.search(userId, null, null, "递归");

        assertThat(hits).extracting(MemoryResponse::id).containsExactly(memoryId);
    }

    @Test
    @DisplayName("F2 英文关键词：ngram 同样能召回英文内容")
    void englishKeywordFindsMemory() {
        Long memoryId = memoryService.create(userId,
                request(null, null, "ft/en", "prefers recursion over iteration", 0.5f)).id();

        List<MemoryResponse> hits = memoryService.search(userId, null, null, "recursion");

        assertThat(hits).extracting(MemoryResponse::id).contains(memoryId);
    }

    @Test
    @DisplayName("F3 长句查询：用户原话（含多个词）能命中其中任意二元组")
    void longSentenceQueryStillMatches() {
        Long memoryId = memoryService.create(userId,
                request(null, null, "ft/sentence", "这个人理解递归时总是搞不清基准条件", 0.5f)).id();

        // 调用方传的是用户原话，不是单个关键词；ngram 会把它切成二元组后匹配。
        List<MemoryResponse> hits = memoryService.search(userId, null, null,
                "我不太理解递归和分治的区别");

        assertThat(hits).extracting(MemoryResponse::id).contains(memoryId);
    }

    // ===================== 过滤条件 =====================

    @Test
    @DisplayName("F4 user_id 隔离：其他用户的同名内容不会被召回")
    void otherUsersMemoryIsNeverReturned() {
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "fulltext-other-user", "x");
        Long otherUserId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "fulltext-other-user");
        memoryService.create(otherUserId,
                request(null, null, "ft/other-user", "别人的递归笔记", 0.5f));

        assertThat(memoryService.search(userId, null, null, "递归")).isEmpty();
    }

    @Test
    @DisplayName("F5 status 过滤：CANDIDATE 与 ARCHIVED 不参与检索")
    void onlyActiveMemoriesAreSearchable() {
        Long activeId = memoryService.create(userId,
                request(null, null, "ft/active", "递归的基准条件", 0.5f)).id();
        memoryService.createCandidate(userId,
                request(null, null, "ft/candidate", "递归的候选理解", 0.5f),
                MemorySourceType.MESSAGE, 1L);

        Long archivedId = memoryService.create(userId,
                request(null, null, "ft/archived", "递归的旧笔记", 0.5f)).id();
        memoryService.archive(userId, archivedId);

        List<MemoryResponse> hits = memoryService.search(userId, null, null, "递归");

        assertThat(hits).extracting(MemoryResponse::id).containsExactly(activeId);
    }

    // ===================== 可见性 =====================

    @Test
    @DisplayName("F6 可见性：GLOBAL 任意空间可见；SPACE 仅本空间；CONCEPT 需要同时给对 space 与 concept")
    void visibilityMatrixIsPreserved() {
        Long globalId = memoryService.create(userId,
                request(null, null, "ft/global", "递归：全局偏好", 0.5f)).id();
        Long spaceAId = memoryService.create(userId,
                request(spaceId, null, "ft/space-a", "递归：空间 A 的偏好", 0.5f)).id();
        Long spaceBId = memoryService.create(userId,
                request(otherSpaceId, null, "ft/space-b", "递归：空间 B 的偏好", 0.5f)).id();
        Long conceptIdOfA = memoryService.create(userId,
                request(spaceId, conceptId, "ft/concept", "递归：知识点下的偏好", 0.5f)).id();

        // 只给 spaceA：GLOBAL + SPACE(A)；SPACE(B) 与 CONCEPT 都不可见。
        assertThat(memoryService.search(userId, spaceId, null, "递归"))
                .extracting(MemoryResponse::id)
                .containsExactlyInAnyOrder(globalId, spaceAId);

        // spaceA + conceptId：GLOBAL + SPACE(A) + CONCEPT。
        assertThat(memoryService.search(userId, spaceId, conceptId, "递归"))
                .extracting(MemoryResponse::id)
                .containsExactlyInAnyOrder(globalId, spaceAId, conceptIdOfA);

        // 换到 spaceB：GLOBAL + SPACE(B)。
        assertThat(memoryService.search(userId, otherSpaceId, conceptId, "递归"))
                .extracting(MemoryResponse::id)
                .containsExactlyInAnyOrder(globalId, spaceBId);

        // 完全不给 spaceId：不做可见性过滤，该用户全部 ACTIVE 命中。
        assertThat(memoryService.search(userId, null, null, "递归"))
                .extracting(MemoryResponse::id)
                .containsExactlyInAnyOrder(globalId, spaceAId, spaceBId, conceptIdOfA);
    }

    // ===================== 兼容与边界 =====================

    @Test
    @DisplayName("F7 空 keyword：保持旧语义，返回 ACTIVE 列表而不是空结果")
    void blankKeywordKeepsListSemantics() {
        Long low = memoryService.create(userId,
                request(null, null, "ft/low", "低重要度", 0.1f)).id();
        Long high = memoryService.create(userId,
                request(null, null, "ft/high", "高重要度", 0.9f)).id();

        List<MemoryResponse> hits = memoryService.search(userId, null, null, "   ");

        // 旧实现按 importance DESC 排序；空查询必须继续是这个行为。
        assertThat(hits).extracting(MemoryResponse::id).containsExactly(high, low);
    }

    @Test
    @DisplayName("F8 结果上限：宽泛查询受 LIMIT 约束，不会把整表读进内存")
    void resultLimitIsEnforced() {
        for (int i = 0; i < 5; i++) {
            memoryService.create(userId,
                    request(null, null, "ft/limit-" + i, "递归练习 " + i, 0.5f));
        }

        List<Long> ids = memoryMapper.searchFulltext(userId, null, null, "递归", 2)
                .stream().map(m -> m.getId()).toList();

        assertThat(ids).hasSize(2);
    }

    // ===================== helpers =====================

    /** 老库没有 FULLTEXT 索引时补建；同时验证迁移脚本的 DDL 可执行。 */
    private void ensureFulltextIndex() {
        Integer count = jdbc.queryForObject("""
                SELECT COUNT(*)
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = 'memory'
                  AND index_name = 'ft_memory_content'
                """, Integer.class);
        if (count == null || count == 0) {
            jdbc.execute("ALTER TABLE memory ADD FULLTEXT INDEX ft_memory_content (content) "
                    + "WITH PARSER ngram");
        }
    }

    private Long insertSpace(Long ownerId, String name) {
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                ownerId, name);
        return jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    private Long insertConcept(Long learningSpaceId, String canonicalName) {
        jdbc.update("INSERT INTO concept (learning_space_id, canonical_name) VALUES (?, ?)",
                learningSpaceId, canonicalName);
        return jdbc.queryForObject(
                "SELECT id FROM concept WHERE learning_space_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, learningSpaceId);
    }

    private static MemoryRequest request(Long learningSpaceId, Long conceptId,
                                         String memoryKey, String content, float importance) {
        return new MemoryRequest(learningSpaceId, conceptId, MemoryType.LEARNING,
                memoryKey, content, importance, MemoryStatus.ACTIVE);
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
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }
}
