package com.ails.backend.service;

import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.state.LearningStateResponse;
import com.ails.backend.dto.understanding.UnderstandingResponse;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.enums.UnderstandingStatus;
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
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Phase 2 B/E：真实 MySQL 上的 Evidence / Understanding / Revision / Memory 状态与事务。
 *
 * <p>默认跳过。启用：</p>
 * <pre>
 * AILS_PHASE2_IT=true AILS_IT_MYSQL_PASSWORD=... \
 * ./mvnw -Dtest=Phase2RealStateIntegrationTest test
 * </pre>
 *
 * <p>使用独立库 {@code ails_phase2_it}，不连接 {@code ails} 开发库。所有断言都直接读
 * MySQL（JdbcTemplate）或经由服务重新查询，不用内存对象自证。</p>
 *
 * <p><b>这不是 Mock 测试</b>：Evidence、Understanding、Revision、LearningState、Memory
 * 全部走真实 Mapper 和事务。测试上下文刻意不加载 Controller / Security / Agent。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_PHASE2_IT", matches = "true")
@SpringBootTest(
        classes = Phase2RealStateIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql"
        })
class Phase2RealStateIntegrationTest {

    @Autowired
    private EvidenceService evidenceService;

    @Autowired
    private UnderstandingService understandingService;

    @Autowired
    private LearningStateService learningStateService;

    @Autowired
    private MemoryService memoryService;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private PlatformTransactionManager transactionManager;

    private Long userId;
    private Long spaceId;
    private Long conceptId;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_PHASE2_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_phase2_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
    }

    @BeforeEach
    void setUp() {
        // 只清本测试独立库；顺序满足外键依赖。
        for (String table : List.of(
                "evidence_source", "understanding_revision", "learning_evidence",
                "learning_understanding", "learning_state", "memory_source", "memory",
                "agent_run", "message", "conversation", "concept", "learning_space", "user")) {
            jdbc.update("DELETE FROM `" + table + "`");
        }
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "phase2-user", "x");
        userId = jdbc.queryForObject("SELECT id FROM user WHERE username = ?", Long.class, "phase2-user");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "phase2-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
        jdbc.update("INSERT INTO concept (learning_space_id, canonical_name, display_name) VALUES (?, ?, ?)",
                spaceId, "Phase2_TLB", "TLB");
        conceptId = jdbc.queryForObject(
                "SELECT id FROM concept WHERE learning_space_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, spaceId);
    }

    // ===================== B1 Evidence 去重 =====================

    @Test
    @DisplayName("B1 同一观察重复分析只留一条 Evidence，并推进 last_seen_run_id")
    void evidenceDedupeOnRealDatabase() {
        EvidenceService.Draft draft = draft("TLB 缓存的是页表项", EvidenceCorrectness.PARTIAL, 101L);

        EvidenceService.AppendResult first = evidenceService.append(draft);
        EvidenceService.AppendResult second = evidenceService.append(draft("TLB 缓存的是页表项",
                EvidenceCorrectness.PARTIAL, 202L, List.of(
                        new EvidenceService.SourceRef(EvidenceSourceType.MESSAGE, 9001L),
                        new EvidenceService.SourceRef(EvidenceSourceType.HIGHLIGHT, 777L))));

        assertThat(first.created()).isTrue();
        assertThat(second.created()).isFalse();
        assertThat(second.evidence().getId()).isEqualTo(first.evidence().getId());
        assertThat(count("learning_evidence")).isEqualTo(1);
        Map<String, Object> row = jdbc.queryForMap(
                "SELECT first_seen_run_id, last_seen_run_id FROM learning_evidence WHERE id = ?",
                first.evidence().getId());
        assertThat(((Number) row.get("first_seen_run_id")).longValue()).isEqualTo(101L);
        assertThat(((Number) row.get("last_seen_run_id")).longValue()).isEqualTo(202L);
        // 来源不会因为去重被丢掉
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM evidence_source WHERE evidence_id = ?",
                Integer.class, first.evidence().getId())).isEqualTo(2);
    }

    // ===================== B2 内容不变不产生 Revision =====================

    @Test
    @DisplayName("B2 相同内容重复 apply 不新增 Revision，也不推进快照 updated_at")
    void identicalUnderstandingDoesNotCreateRevision() {
        LearningEvidence evidence = evidenceService.append(
                draft("TLB 缓存页表项", EvidenceCorrectness.CORRECT, 301L)).evidence();

        UnderstandingService.ApplyResult first = understandingService.apply(
                userId, conceptId, evidence, null);
        assertThat(first.revision()).isNotNull();
        assertThat(first.revision().getRevisionNo()).isEqualTo(1);
        LocalDateTime updatedAfterFirst = understandingService.find(userId, conceptId).getUpdatedAt();

        UnderstandingService.ApplyResult second = understandingService.apply(
                userId, conceptId, evidence, null);

        assertThat(second.revision()).isNull();
        assertThat(count("understanding_revision")).isEqualTo(1);
        assertThat(understandingService.find(userId, conceptId).getUpdatedAt())
                .isEqualTo(updatedAfterFirst);
    }

    // ===================== B3 快照与 Revision 一致 =====================

    @Test
    @DisplayName("B3 成功变更后 Revision 的内容与时间同当前快照一致")
    void revisionMatchesSnapshotAfterRealChange() {
        LearningEvidence correct = evidenceService.append(
                draft("TLB 缓存页表项", EvidenceCorrectness.CORRECT, 401L)).evidence();
        understandingService.apply(userId, conceptId, correct, "缓存页表项");

        LearningEvidence wrong = evidenceService.append(
                draft("TLB 就是页表本身", EvidenceCorrectness.INCORRECT, 402L)).evidence();
        UnderstandingService.ApplyResult changed = understandingService.apply(
                userId, conceptId, wrong, "TLB 就是页表本身");

        assertThat(changed.revision()).isNotNull();
        Map<String, Object> snapshot = jdbc.queryForMap(
                "SELECT summary, known_parts, misconceptions, uncertain_parts, status, updated_at "
                        + "FROM learning_understanding WHERE user_id = ? AND concept_id = ?",
                userId, conceptId);
        Map<String, Object> revision = jdbc.queryForMap(
                "SELECT summary, known_parts, misconceptions, uncertain_parts, status, created_at "
                        + "FROM understanding_revision WHERE id = ?",
                changed.revision().getId());

        assertThat(revision.get("summary")).isEqualTo(snapshot.get("summary"));
        assertThat(revision.get("known_parts").toString()).isEqualTo(snapshot.get("known_parts").toString());
        assertThat(revision.get("misconceptions").toString())
                .isEqualTo(snapshot.get("misconceptions").toString());
        assertThat(revision.get("uncertain_parts").toString())
                .isEqualTo(snapshot.get("uncertain_parts").toString());
        assertThat(revision.get("status")).isEqualTo(snapshot.get("status"));
        assertThat(toLocalDateTime(revision.get("created_at")))
                .isEqualTo(toLocalDateTime(snapshot.get("updated_at")));
        assertThat(changed.understanding().getCurrentRevisionId())
                .isEqualTo(changed.revision().getId());
    }

    // ===================== B4 事务回滚 =====================

    @Test
    @DisplayName("B4 外层事务回滚后不留下半完成 Evidence / Understanding")
    void transactionRollbackLeavesNoPartialState() {
        TransactionTemplate tx = new TransactionTemplate(transactionManager);

        tx.executeWithoutResult(status -> {
            LearningEvidence evidence = evidenceService.append(
                    draft("回滚检查", EvidenceCorrectness.PARTIAL, 501L)).evidence();
            understandingService.apply(userId, conceptId, evidence, "回滚检查");
            assertThat(evidenceService.countByConcept(userId, conceptId)).isEqualTo(1);
            status.setRollbackOnly();
        });

        assertThat(count("learning_evidence")).isZero();
        assertThat(count("learning_understanding")).isZero();
        assertThat(count("understanding_revision")).isZero();
        assertThat(count("learning_state")).isZero();
    }

    // ===================== B5 Memory 生命周期 =====================

    @Test
    @DisplayName("B5 用户 Memory 直接 ACTIVE，Agent 候选只能 CANDIDATE")
    void memoryLifecyclePersistsRealStatus() {
        memoryService.create(userId, new MemoryRequest(
                spaceId, null, MemoryType.PREFERENCE, "phase2/style",
                "偏好先看例子", 0.8f, null));
        var candidate = memoryService.createCandidate(userId, new MemoryRequest(
                spaceId, null, MemoryType.PREFERENCE, "phase2/inferred",
                "可能偏好图例", 0.4f, null), com.ails.backend.enums.MemorySourceType.MESSAGE, 88L);

        assertThat(jdbc.queryForObject(
                "SELECT status FROM memory WHERE memory_key = ?", String.class, "phase2/style"))
                .isEqualTo("ACTIVE");
        assertThat(jdbc.queryForObject(
                "SELECT status FROM memory WHERE id = ?", String.class, candidate.getId()))
                .isEqualTo("CANDIDATE");
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM memory_source WHERE memory_id = ?", Integer.class, candidate.getId()))
                .isEqualTo(1);
    }

    // ===================== E 跨会话持久化 =====================

    @Test
    @DisplayName("E1 理解与证据落库后，重新查询（模拟新会话）仍可读取且来源一致")
    void learningStateSurvivesNewSession() {
        LearningEvidence correct = evidenceService.append(
                draft("TLB 缓存页表项", EvidenceCorrectness.CORRECT, 601L)).evidence();
        UnderstandingService.ApplyResult applied = understandingService.apply(
                userId, conceptId, correct, "缓存的是页表项");
        learningStateService.project(userId, conceptId, applied.understanding(), LocalDateTime.now());

        // 不持有上一会话的任何对象，全部经服务重新查库
        UnderstandingResponse understanding = understandingService.get(userId, conceptId);
        LearningStateResponse state = learningStateService.conceptDetail(userId, conceptId);
        var evidences = evidenceService.listByConcept(userId, conceptId);

        assertThat(understanding).isNotNull();
        assertThat(understanding.knownParts()).contains("TLB 缓存页表项");
        assertThat(understanding.latestUserExplanation()).isEqualTo("缓存的是页表项");
        assertThat(understanding.status()).isIn(
                UnderstandingStatus.EXPOSED, UnderstandingStatus.PARTIAL, UnderstandingStatus.DEMONSTRATED);
        assertThat(evidences).hasSize(1);
        assertThat(evidences.get(0).sources()).hasSize(1);
        assertThat(state.conceptId()).isEqualTo(conceptId);
        assertThat(state.status()).isEqualTo(understanding.status());
    }

    // ===================== helpers =====================

    private EvidenceService.Draft draft(String observation, EvidenceCorrectness correctness, long runId) {
        return draft(observation, correctness, runId,
                List.of(new EvidenceService.SourceRef(EvidenceSourceType.MESSAGE, 9001L)));
    }

    private EvidenceService.Draft draft(String observation, EvidenceCorrectness correctness, long runId,
                                        List<EvidenceService.SourceRef> sources) {
        return new EvidenceService.Draft(
                userId, conceptId, null, EvidenceType.USER_EXPLANATION,
                observation, correctness, null, null, null, null,
                runId, "phase2-test", sources);
    }

    private static LocalDateTime toLocalDateTime(Object value) {
        return value instanceof LocalDateTime ldt ? ldt
                : ((java.sql.Timestamp) value).toLocalDateTime();
    }

    private int count(String table) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM `" + table + "`", Integer.class);
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
            EvidenceService.class,
            UnderstandingService.class,
            LearningStateService.class,
            MemoryService.class,
            ConceptService.class,
            LearningSpaceService.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }
}
