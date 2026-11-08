package com.ails.backend.service;

import com.ails.backend.config.MemoryOutboxProperties;
import com.ails.backend.dto.memory.agent.MemoryEventPayload;
import com.ails.backend.dto.memory.agent.MemorySyncRequest;
import com.ails.backend.entity.OutboxEvent;
import com.ails.backend.enums.MemoryEventType;
import com.ails.backend.mapper.OutboxEventMapper;
import com.ails.backend.utils.JsonUtil;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.net.InetAddress;
import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Outbox Relay：把 {@code outbox_event} 中的 Memory 变更异步投递到 Agent。
 *
 * <h3>并发与乱序</h3>
 * 领取 SQL 内置 head-of-line：同一 {@code aggregate_id} 若还有更早的
 * {@code PENDING/PROCESSING} 事件，后面的不会被选中。这是正确性要求，不是优化 ——
 * 否则失败重试的旧事件会把索引写回旧内容。
 *
 * <h3>抢占与租约</h3>
 * 候选查询之后用条件 {@code UPDATE ... WHERE status='PENDING'} 领取；多实例部署时
 * 只有一个实例能成功。租约超时的事件在重试未耗尽时回到 PENDING，
 * {@code attempt_count} 不重置；已耗尽的直接进入 DEAD，避免无限重试。
 *
 * <h3>失败分类</h3>
 * {@link AgentMemoryException#isPermanent()} 为真（4xx，408/429 除外，以及契约漂移）
 * 直接进入 DEAD；其余按指数退避重试，超过 {@code max_attempts} 进入 DEAD。
 * 所有回写都带 {@code locked_by}（fencing），租约被回收后旧实例不能覆盖新结果。
 */
@Slf4j
@Component
public class OutboxRelayWorker {

    static final String AGGREGATE_TYPE = "MEMORY";
    private static final int ERROR_MAX_LENGTH = 500;
    private static final int CLEANUP_RETENTION_DAYS = 7;
    private static final String LEASE_EXPIRED_DEAD_REASON =
            "租约过期且重试次数已耗尽（Worker 可能在异常回写前崩溃）";

    private final OutboxEventMapper outboxEventMapper;
    private final AgentMemoryClient agentMemoryClient;
    private final MemoryOutboxProperties properties;
    private final String workerId;

    public OutboxRelayWorker(OutboxEventMapper outboxEventMapper,
                             AgentMemoryClient agentMemoryClient,
                             MemoryOutboxProperties properties) {
        this.outboxEventMapper = outboxEventMapper;
        this.agentMemoryClient = agentMemoryClient;
        this.properties = properties;
        this.workerId = buildWorkerId();
    }

    @Scheduled(
            fixedDelayString = "${ails.memory.outbox.interval-ms:2000}",
            initialDelayString = "${ails.memory.outbox.initial-delay-ms:10000}")
    public void relay() {
        if (!properties.relayEnabled()) {
            return;
        }
        try {
            relayTick();
        } catch (RuntimeException e) {
            // 调度线程不能因为一次异常停摆；下一轮继续。
            log.error("memory.outbox.relay.failed workerId={}", workerId, e);
        }
    }

    /**
     * 执行一轮：回收过期租约 → 按批领取 → 逐条处理，直到没有可领取事件或达到上限。
     *
     * @return 本轮实际领取的事件数
     */
    int relayTick() {
        LocalDateTime now = LocalDateTime.now();
        recoverExpiredLeases(now);

        int processed = 0;
        int maxPerTick = properties.maxBatchPerTick();
        int batchSize = properties.batchSize();
        while (processed < maxPerTick) {
            int claimed = relayOnce();
            processed += claimed;
            if (claimed < batchSize) {
                break;
            }
        }
        if (processed > 0) {
            log.info("memory.outbox.relay processed={} workerId={}", processed, workerId);
        }
        return processed;
    }

    /** 单批处理；包可见，便于测试直接驱动。 */
    int relayOnce() {
        LocalDateTime now = LocalDateTime.now();
        List<OutboxEvent> candidates =
                outboxEventMapper.selectClaimable(now, properties.batchSize());
        int processed = 0;
        for (OutboxEvent event : candidates) {
            // 条件 UPDATE 抢占：返回 0 表示别的实例已经拿走，直接跳过。
            if (outboxEventMapper.claim(event.getId(), workerId, now) == 0) {
                continue;
            }
            processed++;
            process(event);
        }
        return processed;
    }

    private void process(OutboxEvent event) {
        MemorySyncRequest request;
        try {
            request = toSyncRequest(event);
        } catch (RuntimeException e) {
            // payload 非法通常是发布端 bug 或版本不匹配；重试不会修复。
            markDead(event, "payload 非法：" + rootMessage(e));
            log.error("memory.outbox.payload-invalid eventId={} memoryId={}",
                    event.getId(), event.getAggregateId(), e);
            return;
        }

        try {
            agentMemoryClient.sync(request);
            if (outboxEventMapper.markDone(event.getId(), workerId, LocalDateTime.now()) == 0) {
                log.warn("memory.outbox.done-lost eventId={} workerId={}",
                        event.getId(), workerId);
                return;
            }
            log.info("memory.outbox.done eventId={} type={} memoryId={}",
                    event.getId(), event.getEventType(), event.getAggregateId());
        } catch (AgentMemoryException e) {
            if (e.isPermanent()) {
                markDead(event, e.getMessage());
                log.error("memory.outbox.dead eventId={} memoryId={} status={}",
                        event.getId(), event.getAggregateId(), e.statusCode());
            } else {
                retryOrDead(event, e);
            }
        } catch (RuntimeException e) {
            retryOrDead(event, e);
        }
    }

    private void retryOrDead(OutboxEvent event, RuntimeException error) {
        int attempts = attemptCount(event);
        int maxAttempts = event.getMaxAttempts() == null
                ? properties.maxAttempts() : event.getMaxAttempts();
        if (attempts >= maxAttempts) {
            markDead(event, "重试耗尽：" + rootMessage(error));
            log.error("memory.outbox.dead eventId={} memoryId={} attempts={}",
                    event.getId(), event.getAggregateId(), attempts);
            return;
        }

        long delayMs = nextDelayMillis(attempts);
        LocalDateTime nextAttemptAt = LocalDateTime.now().plusNanos(delayMs * 1_000_000L);
        int updated = outboxEventMapper.markPending(event.getId(), workerId,
                nextAttemptAt, truncate(rootMessage(error), ERROR_MAX_LENGTH));
        if (updated == 0) {
            log.warn("memory.outbox.retry-lost eventId={} workerId={}",
                    event.getId(), workerId);
            return;
        }
        log.warn("memory.outbox.retry eventId={} memoryId={} attempts={} delayMs={}",
                event.getId(), event.getAggregateId(), attempts, delayMs);
    }

    private void markDead(OutboxEvent event, String error) {
        int updated = outboxEventMapper.markDead(event.getId(), workerId,
                LocalDateTime.now(), truncate(error, ERROR_MAX_LENGTH));
        if (updated == 0) {
            log.warn("memory.outbox.dead-lost eventId={} workerId={}",
                    event.getId(), workerId);
        } else {
            log.error("memory.outbox.dead eventId={} memoryId={} reason={}",
                    event.getId(), event.getAggregateId(), truncate(error, 120));
        }
    }

    private void recoverExpiredLeases(LocalDateTime now) {
        LocalDateTime cutoff = now.minusSeconds(properties.leaseSeconds());

        // 先处理重试已耗尽的事件：它们在异常回写之前崩溃，租约过期后不能回到
        // PENDING，否则会「领取 → 崩溃 → 回收 → 再领取」无限循环。
        int deadLettered = outboxEventMapper.deadLetterExpiredLeases(
                cutoff, now, LEASE_EXPIRED_DEAD_REASON);
        if (deadLettered > 0) {
            log.error("memory.outbox.lease-dead count={} cutoff={} reason={}",
                    deadLettered, cutoff, LEASE_EXPIRED_DEAD_REASON);
        }

        int recovered = outboxEventMapper.recoverExpiredLeases(cutoff, now);
        if (recovered > 0) {
            log.warn("memory.outbox.lease-recovered count={} cutoff={}", recovered, cutoff);
        }
    }

    /**
     * 每天清理 7 天前完成的事件。DEAD 不自动删除：保留 payload 供人工重放。
     */
    @Scheduled(
            fixedDelayString = "${ails.memory.outbox.cleanup-interval-ms:86400000}",
            initialDelayString = "${ails.memory.outbox.cleanup-initial-delay-ms:60000}")
    public void cleanup() {
        if (!properties.relayEnabled()) {
            return;
        }
        try {
            int deleted = outboxEventMapper.deleteDoneBefore(
                    LocalDateTime.now().minusDays(CLEANUP_RETENTION_DAYS),
                    properties.cleanupBatchSize());
            if (deleted > 0) {
                log.info("memory.outbox.cleanup deleted={}", deleted);
            }
        } catch (RuntimeException e) {
            log.error("memory.outbox.cleanup.failed", e);
        }
    }

    // ===================== 契约转换 =====================

    private MemorySyncRequest toSyncRequest(OutboxEvent event) {
        if (event == null || event.getAggregateId() == null) {
            throw new IllegalArgumentException("事件缺少 aggregateId");
        }
        if (!AGGREGATE_TYPE.equals(event.getAggregateType())) {
            throw new IllegalArgumentException("不支持的 aggregateType: " + event.getAggregateType());
        }

        MemoryEventPayload payload = JsonUtil.fromJson(event.getPayload(), MemoryEventPayload.class);
        if (payload == null) {
            throw new IllegalArgumentException("payload 为空");
        }
        if (payload.memoryId() == null
                || !payload.memoryId().equals(event.getAggregateId())) {
            throw new IllegalArgumentException("payload.memoryId 与 aggregateId 不一致");
        }
        // 显式要求 userId 与 scopeType：不信任下游推导，也不允许事件缺字段后
        // 以 GLOBAL/无主身份写入向量库。
        if (payload.userId() == null || payload.userId() <= 0) {
            throw new IllegalArgumentException("payload 缺少 userId");
        }
        if (!StringUtils.hasText(payload.scopeType())) {
            throw new IllegalArgumentException("payload 缺少 scopeType");
        }

        MemoryEventType type;
        try {
            type = MemoryEventType.valueOf(event.getEventType());
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("不支持的事件类型: " + event.getEventType());
        }

        String operation = type == MemoryEventType.MEMORY_UPSERT ? "UPSERT" : "DELETE";
        if (type == MemoryEventType.MEMORY_UPSERT && !StringUtils.hasText(payload.content())) {
            throw new IllegalArgumentException("UPSERT 事件缺少 content");
        }

        return new MemorySyncRequest(
                event.getId(),
                operation,
                payload.memoryId(),
                payload.userId(),
                payload.content(),
                payload.contentHash(),
                payload.memoryVersion(),
                payload.scopeType(),
                payload.learningSpaceId(),
                payload.conceptId(),
                payload.memoryType(),
                payload.importance(),
                payload.status());
    }

    private int attemptCount(OutboxEvent event) {
        Integer current = event.getAttemptCount();
        // 领取时 DB 已 +1，但内存对象仍是领取前的值；这里补上本次尝试。
        return (current == null ? 0 : current) + 1;
    }

    private long nextDelayMillis(int attempt) {
        long base = baseBackoffMillis(attempt,
                properties.baseDelayMs(), properties.maxDelayMs());
        return withJitter(base, properties.jitterRatio(),
                ThreadLocalRandom.current().nextDouble());
    }

    /**
     * 纯函数：{@code min(base * 2^(attempt-1), max)}。attempt 从 1 开始。
     */
    static long baseBackoffMillis(int attempt, long baseMs, long maxMs) {
        int shift = Math.max(0, attempt - 1);
        // 位移过大直接封顶，避免 long 溢出后再 min 得到负数。
        if (shift >= 30) {
            return maxMs;
        }
        long raw = baseMs * (1L << shift);
        if (raw <= 0) {
            return maxMs;
        }
        return Math.min(raw, maxMs);
    }

    /**
     * 纯函数：对延迟施加 ±ratio 抖动。{@code randomUnit} ∈ [0,1)。
     */
    static long withJitter(long delayMs, double ratio, double randomUnit) {
        if (ratio <= 0 || delayMs <= 0) {
            return delayMs;
        }
        double factor = 1.0 + (Math.min(Math.max(randomUnit, 0.0), 1.0) * 2 - 1) * ratio;
        return Math.max(0L, Math.round(delayMs * factor));
    }

    private static String buildWorkerId() {
        String host;
        try {
            host = InetAddress.getLocalHost().getHostName();
        } catch (RuntimeException | java.net.UnknownHostException e) {
            host = "relay";
        }
        String id = host + "-" + UUID.randomUUID().toString().substring(0, 8);
        return id.length() <= 64 ? id : id.substring(0, 64);
    }

    private static String rootMessage(Throwable error) {
        Throwable cause = error;
        while (cause.getCause() != null && cause.getCause() != cause) {
            cause = cause.getCause();
        }
        String message = cause.getMessage();
        return message == null || message.isBlank()
                ? cause.getClass().getSimpleName() : message;
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
