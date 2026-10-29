package com.ails.backend.mq;

import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.MessageMapper;
import com.ails.backend.service.AgentAnalysisWriteService;
import com.ails.backend.service.ConceptService;
import com.ails.backend.service.EvidenceService;
import com.ails.backend.service.LearningAnalysisService;
import com.ails.backend.service.LearningSpaceService;
import com.ails.backend.service.LearningStateService;
import com.ails.backend.service.MemoryService;
import com.ails.backend.service.UnderstandingService;
import com.ails.backend.service.VerificationService;
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
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.when;

/**
 * Phase 2 C：真实 MySQL 上分析结果消息的幂等、失败终态与并发重复投递。
 *
 * <pre>
 * AILS_PHASE2_IT=true AILS_IT_MYSQL_PASSWORD=... \
 * ./mvnw -Dtest=Phase2MqProcessorIntegrationTest test
 * </pre>
 *
 * <p>RabbitMQ 本身由 {@code AnalysisMqIntegrationTest}（Testcontainers）覆盖；这里验证
 * 消费后的<b>数据库最终状态</b>。MemoryService / VerificationService 用 Mock 隔离，
 * 因为本用例只产生 Evidence + Understanding，不产生 Memory / 题目。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_PHASE2_IT", matches = "true")
@SpringBootTest(
        classes = Phase2MqProcessorIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql",
                "ails.analysis.mode=mq"
        })
class Phase2MqProcessorIntegrationTest {

    @Autowired
    private AnalysisResultProcessor processor;

    @Autowired
    private AgentRunMapper agentRunMapper;

    @Autowired
    private ConversationMapper conversationMapper;

    @Autowired
    private MessageMapper messageMapper;

    @Autowired
    private JdbcTemplate jdbc;

    @MockBean
    private MemoryService memoryService;

    @MockBean
    private VerificationService verificationService;

    @MockBean
    private LearningAnalysisService learningAnalysisService;

    private Long userId;
    private Long spaceId;
    private Long conceptId;
    private Long conversationId;
    private Long messageId;

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
        for (String table : List.of(
                "evidence_source", "understanding_revision", "learning_evidence",
                "learning_understanding", "learning_state", "memory_source", "memory",
                "agent_run", "message", "conversation", "concept", "learning_space", "user")) {
            jdbc.update("DELETE FROM `" + table + "`");
        }
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "phase2-mq", "x");
        userId = jdbc.queryForObject("SELECT id FROM user WHERE username = ?", Long.class, "phase2-mq");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "phase2-mq-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
        jdbc.update("INSERT INTO concept (learning_space_id, canonical_name, display_name) VALUES (?, ?, ?)",
                spaceId, "Phase2_MQ_TLB", "TLB");
        conceptId = jdbc.queryForObject(
                "SELECT id FROM concept WHERE learning_space_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, spaceId);

        jdbc.update("INSERT INTO conversation (user_id, learning_space_id, title) VALUES (?, ?, ?)",
                userId, spaceId, "phase2");
        conversationId = jdbc.queryForObject(
                "SELECT id FROM conversation WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
        jdbc.update("INSERT INTO message (conversation_id, role, content, sequence_no) "
                        + "VALUES (?, 'USER', ?, 1)",
                conversationId, "TLB 缓存页表项，我认为 TLB 就是页表");
        messageId = jdbc.queryForObject(
                "SELECT id FROM message WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, conversationId);
    }

    @Test
    @DisplayName("C1 成功结果落库一次；重复投递被识别为 DUPLICATE，业务表不重复")
    void duplicateDeliveryIsAckedWithoutDuplicateRows() {
        AgentRun run = insertRun(AgentRunStatus.PENDING);

        AnalysisResultProcessor.Outcome first = processor.process(successMessage(run));
        AnalysisResultProcessor.Outcome second = processor.process(successMessage(run));

        assertThat(first).isEqualTo(AnalysisResultProcessor.Outcome.APPLIED);
        assertThat(second).isEqualTo(AnalysisResultProcessor.Outcome.DUPLICATE);
        assertThat(runStatus(run.getId())).isEqualTo("SUCCEEDED");
        assertThat(count("learning_evidence")).isEqualTo(1);
        assertThat(count("learning_understanding")).isEqualTo(1);
        assertThat(count("understanding_revision")).isEqualTo(1);
        assertThat(count("learning_state")).isEqualTo(1);
    }

    @Test
    @DisplayName("C2 同一结果并发投递：只留一条 Evidence / Revision，run 收敛到 SUCCEEDED")
    void concurrentDuplicateDeliveryDoesNotDuplicateBusinessRows() throws Exception {
        AgentRun run = insertRun(AgentRunStatus.PENDING);
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Callable<AnalysisResultProcessor.Outcome> task = () -> {
                start.await();
                return processor.process(successMessage(run));
            };
            Future<AnalysisResultProcessor.Outcome> a = pool.submit(task);
            Future<AnalysisResultProcessor.Outcome> b = pool.submit(task);
            start.countDown();
            AnalysisResultProcessor.Outcome oa = a.get(30, TimeUnit.SECONDS);
            AnalysisResultProcessor.Outcome ob = b.get(30, TimeUnit.SECONDS);

            assertThat(List.of(oa, ob))
                    .contains(AnalysisResultProcessor.Outcome.APPLIED)
                    .doesNotContain(AnalysisResultProcessor.Outcome.IGNORED);
        } finally {
            pool.shutdownNow();
        }

        assertThat(runStatus(run.getId())).isEqualTo("SUCCEEDED");
        assertThat(count("learning_evidence")).isEqualTo(1);
        assertThat(count("learning_understanding")).isEqualTo(1);
        assertThat(count("understanding_revision")).isEqualTo(1);
    }

    @Test
    @DisplayName("C3 失败结果把 run 置 FAILED，且不写任何 Evidence")
    void failedResultMarksRunFailedWithoutBusinessRows() {
        AgentRun run = insertRun(AgentRunStatus.PENDING);

        AnalysisResultProcessor.Outcome outcome = processor.process(new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION, run.getId(), run.getTraceId(),
                messageId, null, AnalysisResultMessage.Status.FAILED,
                null, "LLM_UNAVAILABLE", "model down", true));

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.FAILED);
        assertThat(runStatus(run.getId())).isEqualTo("FAILED");
        assertThat(count("learning_evidence")).isZero();
        assertThat(count("learning_understanding")).isZero();
    }

    @Test
    @DisplayName("C4 写入中途失败时整笔回滚：Evidence 不残留，run 不被置成 RUNNING/SUCCEEDED")
    void writeFailureRollsBackEvidenceAndClaim() {
        AgentRun run = insertRun(AgentRunStatus.PENDING);
        when(memoryService.reconcileCandidate(anyLong(), any(), any(), any()))
                .thenThrow(new IllegalStateException("memory write failed"));
        com.ails.backend.dto.agent.AnalyzeAgentResult failing = new com.ails.backend.dto.agent.AnalyzeAgentResult(
                successResult().evidenceCandidates(),
                successResult().understandingProposal(),
                List.of(new com.ails.backend.dto.agent.AnalyzeAgentResult.MemoryCandidate(
                        MemoryScopeType.GLOBAL, null, null, MemoryType.PREFERENCE,
                        "phase2/rollback", "回滚测试", 0.5f, MemoryReconcileAction.ADD)),
                com.ails.backend.dto.agent.AnalyzeAgentResult.VerificationDecision.skip(),
                "phase2-test", List.of("Phase2_MQ_TLB"));

        assertThatThrownBy(() -> processor.process(new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION, run.getId(), run.getTraceId(),
                messageId, null, AnalysisResultMessage.Status.SUCCEEDED,
                failing, null, null, null)))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("memory write failed");

        assertThat(count("learning_evidence")).isZero();
        assertThat(count("learning_understanding")).isZero();
        assertThat(count("understanding_revision")).isZero();
        assertThat(runStatus(run.getId())).isEqualTo("PENDING");
    }

    @Test
    @DisplayName("C5 traceId 不一致的结果被拒绝，run 置 FAILED，业务表无写入")
    void traceMismatchWritesNothing() {
        AgentRun run = insertRun(AgentRunStatus.PENDING);

        AnalysisResultProcessor.Outcome outcome = processor.process(new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION, run.getId(), "ffffffffffffffffffffffffffffffff",
                messageId, null, AnalysisResultMessage.Status.SUCCEEDED,
                successResult(), null, null, null));

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.FAILED);
        assertThat(runStatus(run.getId())).isEqualTo("FAILED");
        assertThat(count("learning_evidence")).isZero();
    }

    // ===================== helpers =====================

    private AgentRun insertRun(AgentRunStatus status) {
        AgentRun run = new AgentRun();
        run.setUserId(userId);
        run.setConversationId(conversationId);
        run.setRunType(AgentRunType.ANALYSIS);
        run.setStatus(status);
        run.setTraceId("0123456789abcdef0123456789abcdef");
        run.setInputSummary("phase2");
        agentRunMapper.insert(run);
        return run;
    }

    private AnalysisResultMessage successMessage(AgentRun run) {
        return new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION, run.getId(), run.getTraceId(),
                messageId, null, AnalysisResultMessage.Status.SUCCEEDED,
                successResult(), null, null, null);
    }

    private AnalyzeAgentResult successResult() {
        return new AnalyzeAgentResult(
                List.of(new AnalyzeAgentResult.EvidenceCandidate(
                        conceptId, EvidenceType.USER_EXPLANATION,
                        "TLB 缓存页表项", EvidenceCorrectness.PARTIAL,
                        null, null, null, null, "TLB 缓存页表项")),
                new AnalyzeAgentResult.UnderstandingProposal(
                        conceptId, List.of("TLB 缓存页表项"), List.of(), List.of(), "TLB 缓存页表项"),
                List.of(), AnalyzeAgentResult.VerificationDecision.skip(),
                "phase2-test", List.of("Phase2_MQ_TLB"));
    }

    private String runStatus(Long runId) {
        return jdbc.queryForObject("SELECT status FROM agent_run WHERE id = ?", String.class, runId);
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
            AnalysisResultProcessor.class,
            AgentAnalysisWriteService.class,
            EvidenceService.class,
            UnderstandingService.class,
            LearningStateService.class,
            ConceptService.class,
            LearningSpaceService.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }
}
