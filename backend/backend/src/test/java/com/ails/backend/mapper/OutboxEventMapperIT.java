package com.ails.backend.mapper;

import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.entity.OutboxEvent;
import com.ails.backend.enums.OutboxEventStatus;
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

import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Outbox 领取 SQL 的真实 MySQL 验证（默认跳过）。
 *
 * <p><b>这是正确性测试，不是性能测试</b>：head-of-line 顺序、条件领取、
 * 租约回收与 fencing 都依赖 SQL 语义，只有真实数据库能证明。</p>
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_MEMORY_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dit.test=OutboxEventMapperIT verify
 * </pre>
 */
@EnabledIfEnvironmentVariable(named = "AILS_MEMORY_IT", matches = "true")
@SpringBootTest(
        classes = OutboxEventMapperIT.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql"
        })
class OutboxEventMapperIT {

    private static final String WORKER_A = "worker-a";
    private static final String WORKER_B = "worker-b";
    private static final long MEMORY_ID = 100L;

    @Autowired
    private OutboxEventMapper mapper;

    @Autowired
    private JdbcTemplate jdbc;

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
    void clean() {
        jdbc.update("DELETE FROM outbox_event");
    }

    @Test
    @DisplayName("Head-of-line：较早事件未完成时，较晚事件即使已到期也不能被领取")
    void laterEventWaitsForEarlierEvent() {
        LocalDateTime now = LocalDateTime.now().withNano(0);
        long earlier = insert("MEMORY_UPSERT", MEMORY_ID, now.plusHours(1)); // 尚未到期
        long later = insert("MEMORY_DELETE", MEMORY_ID, now.minusMinutes(1)); // 已到期

        // 较晚事件虽然到期，但同聚合还有更早的 PENDING，必须等待。
        assertThat(mapper.selectClaimable(now, 100)).isEmpty();

        // 让较早事件到期：下一轮只能领取较早事件。
        makeDue(earlier, now.minusSeconds(1));
        List<OutboxEvent> claimed = mapper.selectClaimable(now, 100);
        assertThat(claimed).extracting(OutboxEvent::getId).containsExactly(earlier);

        assertThat(mapper.claim(earlier, WORKER_A, now)).isEqualTo(1);
        assertThat(mapper.markDone(earlier, WORKER_A, now)).isEqualTo(1);

        // 较早事件终态后，较晚事件立刻可领取 —— 顺序被纠正而不是被跳过。
        assertThat(mapper.selectClaimable(now, 100))
                .extracting(OutboxEvent::getId)
                .containsExactly(later);
    }

    @Test
    @DisplayName("条件领取：同一事件只有第一个 worker 能抢到，attempt_count 只加一次")
    void claimIsExclusive() {
        LocalDateTime now = LocalDateTime.now().withNano(0);
        long id = insert("MEMORY_UPSERT", MEMORY_ID, now.minusSeconds(1));

        assertThat(mapper.claim(id, WORKER_A, now)).isEqualTo(1);
        assertThat(mapper.claim(id, WORKER_B, now)).isZero();
        assertThat(attemptCount(id)).isEqualTo(1);
    }

    @Test
    @DisplayName("重试：markPending 写入的 next_attempt_at 之前不可再领取，之后恢复")
    void retryDelayBlocksReclaimUntilDue() {
        LocalDateTime now = LocalDateTime.now().withNano(0);
        long id = insert("MEMORY_UPSERT", MEMORY_ID, now.minusSeconds(1));
        assertThat(mapper.claim(id, WORKER_A, now)).isEqualTo(1);

        LocalDateTime retryAt = now.plusMinutes(5);
        assertThat(mapper.markPending(id, WORKER_A, retryAt, "timeout")).isEqualTo(1);

        assertThat(mapper.selectClaimable(now, 100)).isEmpty();
        assertThat(mapper.selectClaimable(now.plusMinutes(6), 100))
                .extracting(OutboxEvent::getId)
                .containsExactly(id);
    }

    @Test
    @DisplayName("租约回收：过期 PROCESSING 回到 PENDING，attempt_count 不重置")
    void expiredLeaseReturnsToPendingWithoutResettingAttempts() {
        LocalDateTime now = LocalDateTime.now().withNano(0);
        long id = insert("MEMORY_UPSERT", MEMORY_ID, now.minusSeconds(1));
        assertThat(mapper.claim(id, WORKER_A, now)).isEqualTo(1);

        int recovered = mapper.recoverExpiredLeases(now.plusSeconds(1), now.plusSeconds(1));
        assertThat(recovered).isEqualTo(1);

        OutboxEvent row = mapper.selectById(id);
        assertThat(row.getStatus()).isEqualTo(OutboxEventStatus.PENDING);
        assertThat(row.getLockedBy()).isNull();
        assertThat(row.getAttemptCount()).isEqualTo(1);

        // Fencing：旧 worker 不能再回写已被回收的租约。
        assertThat(mapper.markDone(id, WORKER_A, now.plusSeconds(2))).isZero();
        assertThat(mapper.markDead(id, WORKER_A, now.plusSeconds(2), "late")).isZero();

        // 新 worker 可以重新领取，attempt_count 继续累加。
        assertThat(mapper.claim(id, WORKER_B, now.plusSeconds(2))).isEqualTo(1);
        assertThat(attemptCount(id)).isEqualTo(2);
    }

    @Test
    @DisplayName("过期租约回收不影响仍在租约内的 PROCESSING")
    void freshLeaseIsNotRecovered() {
        LocalDateTime now = LocalDateTime.now().withNano(0);
        long id = insert("MEMORY_UPSERT", MEMORY_ID, now.minusSeconds(1));
        assertThat(mapper.claim(id, WORKER_A, now)).isEqualTo(1);

        assertThat(mapper.recoverExpiredLeases(now.minusSeconds(60), now)).isZero();
        assertThat(mapper.selectById(id).getStatus()).isEqualTo(OutboxEventStatus.PROCESSING);
    }

    @Test
    @DisplayName("清理：只删除 DONE 且早于保留期的事件，DEAD 保留供人工重放")
    void cleanupDeletesOnlyOldDoneEvents() {
        LocalDateTime now = LocalDateTime.now().withNano(0);
        long done = insert("MEMORY_UPSERT", MEMORY_ID, now.minusSeconds(1));
        mapper.claim(done, WORKER_A, now);
        mapper.markDone(done, WORKER_A, now);

        long dead = insert("MEMORY_UPSERT", MEMORY_ID + 1, now.minusSeconds(1));
        mapper.claim(dead, WORKER_A, now);
        mapper.markDead(dead, WORKER_A, now, "bad payload");

        // completed_at 是 now（毫秒级），cutoff 用 now+1s 保证能覆盖。
        assertThat(mapper.deleteDoneBefore(now.plusSeconds(1), 100)).isEqualTo(1);
        assertThat(mapper.selectById(done)).isNull();
        assertThat(mapper.selectById(dead)).isNotNull();
    }

    // ===================== helpers =====================

    private long insert(String eventType, long memoryId, LocalDateTime nextAttemptAt) {
        OutboxEvent event = new OutboxEvent();
        event.setEventType(eventType);
        event.setAggregateType("MEMORY");
        event.setAggregateId(memoryId);
        event.setPayload("{\"memoryId\":" + memoryId + "}");
        event.setStatus(OutboxEventStatus.PENDING);
        event.setAttemptCount(0);
        event.setMaxAttempts(8);
        event.setNextAttemptAt(nextAttemptAt);
        mapper.insert(event);
        return event.getId();
    }

    private void makeDue(long id, LocalDateTime nextAttemptAt) {
        jdbc.update("UPDATE outbox_event SET next_attempt_at = ? WHERE id = ?",
                Timestamp.valueOf(nextAttemptAt), id);
    }

    private int attemptCount(long id) {
        return mapper.selectById(id).getAttemptCount();
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
    @Import({MybatisPlusConfig.class, MyBatisMetaObjectHandler.class})
    static class TestApplication {
    }
}
