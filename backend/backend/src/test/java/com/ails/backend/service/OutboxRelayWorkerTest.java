package com.ails.backend.service;

import com.ails.backend.config.MemoryOutboxProperties;
import com.ails.backend.dto.memory.agent.MemoryEventPayload;
import com.ails.backend.dto.memory.agent.MemorySyncRequest;
import com.ails.backend.dto.memory.agent.MemorySyncResponse;
import com.ails.backend.entity.OutboxEvent;
import com.ails.backend.enums.MemoryEventType;
import com.ails.backend.enums.OutboxEventStatus;
import com.ails.backend.mapper.OutboxEventMapper;
import com.ails.backend.utils.Hashing;
import com.ails.backend.utils.JsonUtil;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * Outbox Relay 的成功、重试、死信与租约语义单测。
 *
 * <p>不启动调度线程：直接驱动包可见的 {@code relayOnce()}，让每一步可断言。</p>
 */
@ExtendWith(MockitoExtension.class)
class OutboxRelayWorkerTest {

    private static final long MEMORY_ID = 42L;
    private static final long USER_ID = 7L;
    private static final int BATCH_SIZE = 100;

    @Mock
    private OutboxEventMapper outboxEventMapper;
    @Mock
    private AgentMemoryClient agentMemoryClient;

    private MemoryOutboxProperties properties;
    private OutboxRelayWorker worker;

    @BeforeEach
    void setUp() {
        properties = new MemoryOutboxProperties(
                true, 2000L, 10000L, BATCH_SIZE, 1000, 120L, 8,
                2000L, 300_000L, 0.2, 86_400_000L, 60_000L, 1000);
        worker = new OutboxRelayWorker(outboxEventMapper, agentMemoryClient, properties);
    }

    @Test
    @DisplayName("成功：调用 Agent 后标记 DONE，并显式传递 userId / scopeType")
    void successMarksDoneAndSendsExplicitIdentity() {
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(event(MemoryEventType.MEMORY_UPSERT, 0, 8)));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(1);
        when(agentMemoryClient.sync(any()))
                .thenReturn(new MemorySyncResponse("point-1", "memories", false));
        when(outboxEventMapper.markDone(anyLong(), anyString(), any())).thenReturn(1);

        assertThat(worker.relayOnce()).isEqualTo(1);

        ArgumentCaptor<MemorySyncRequest> request =
                ArgumentCaptor.forClass(MemorySyncRequest.class);
        verify(agentMemoryClient).sync(request.capture());
        assertThat(request.getValue().memoryId()).isEqualTo(MEMORY_ID);
        assertThat(request.getValue().userId()).isEqualTo(USER_ID);
        assertThat(request.getValue().scopeType()).isEqualTo("SPACE");
        assertThat(request.getValue().operation()).isEqualTo("UPSERT");
        assertThat(request.getValue().content()).isEqualTo("content");
        verify(outboxEventMapper).markDone(eq(MEMORY_ID), anyString(), any());
        verify(outboxEventMapper, never()).markPending(anyLong(), anyString(), any(), any());
    }

    @Test
    @DisplayName("DELETE 事件：operation=DELETE，不要求 content")
    void deleteEventIsForwarded() {
        OutboxEvent event = event(MemoryEventType.MEMORY_DELETE, 0, 8);
        MemoryEventPayload payload = JsonUtil.fromJson(event.getPayload(), MemoryEventPayload.class);
        event.setPayload(JsonUtil.toJson(new MemoryEventPayload(
                payload.memoryId(), payload.userId(), null, null, payload.memoryVersion(),
                payload.scopeType(), payload.scopeKey(), payload.learningSpaceId(),
                payload.conceptId(), payload.memoryType(), "ARCHIVED", payload.importance(),
                payload.updatedAt())));
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(event));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(1);
        when(agentMemoryClient.sync(any()))
                .thenReturn(new MemorySyncResponse(null, "memories", true));
        when(outboxEventMapper.markDone(anyLong(), anyString(), any())).thenReturn(1);

        worker.relayOnce();

        ArgumentCaptor<MemorySyncRequest> request =
                ArgumentCaptor.forClass(MemorySyncRequest.class);
        verify(agentMemoryClient).sync(request.capture());
        assertThat(request.getValue().operation()).isEqualTo("DELETE");
        assertThat(request.getValue().memoryId()).isEqualTo(MEMORY_ID);
    }

    @Test
    @DisplayName("暂时性失败（504）：写回 PENDING 并安排指数退避，不进入 DEAD")
    void timeoutSchedulesRetryInsteadOfDead() {
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(event(MemoryEventType.MEMORY_UPSERT, 0, 8)));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(1);
        when(agentMemoryClient.sync(any()))
                .thenThrow(new AgentMemoryException(504, "等待 Agent 记忆同步超时"));
        when(outboxEventMapper.markPending(anyLong(), anyString(), any(), anyString()))
                .thenReturn(1);

        LocalDateTime before = LocalDateTime.now();
        worker.relayOnce();

        ArgumentCaptor<LocalDateTime> nextAttempt =
                ArgumentCaptor.forClass(LocalDateTime.class);
        verify(outboxEventMapper).markPending(eq(MEMORY_ID), anyString(),
                nextAttempt.capture(), contains("超时"));
        // attempt=1 → 2000ms ±20%，至少也在 1.5s 之后。
        assertThat(nextAttempt.getValue()).isAfter(before.plusSeconds(1));
        verify(outboxEventMapper, never()).markDead(anyLong(), anyString(), any(), any());
    }

    @Test
    @DisplayName("永久失败（401）：直接 DEAD，不浪费重试次数")
    void permanentErrorMarksDeadImmediately() {
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(event(MemoryEventType.MEMORY_UPSERT, 0, 8)));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(1);
        when(agentMemoryClient.sync(any()))
                .thenThrow(new AgentMemoryException(401, "unauthorized"));
        when(outboxEventMapper.markDead(anyLong(), anyString(), any(), anyString()))
                .thenReturn(1);

        worker.relayOnce();

        verify(outboxEventMapper).markDead(eq(MEMORY_ID), anyString(), any(),
                contains("unauthorized"));
        verify(outboxEventMapper, never()).markPending(anyLong(), anyString(), any(), any());
    }

    @Test
    @DisplayName("重试耗尽：已达到 maxAttempts 时进入 DEAD")
    void exhaustedRetriesMarkDead() {
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(event(MemoryEventType.MEMORY_UPSERT, 7, 8)));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(1);
        when(agentMemoryClient.sync(any()))
                .thenThrow(new AgentMemoryException(503, "Agent down"));
        when(outboxEventMapper.markDead(anyLong(), anyString(), any(), anyString()))
                .thenReturn(1);

        worker.relayOnce();

        verify(outboxEventMapper).markDead(eq(MEMORY_ID), anyString(), any(),
                contains("重试耗尽"));
    }

    @Test
    @DisplayName("租约已被别的实例抢走：claim 返回 0 时跳过，不调用 Agent")
    void lostClaimIsSkipped() {
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(event(MemoryEventType.MEMORY_UPSERT, 0, 8)));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(0);

        assertThat(worker.relayOnce()).isZero();
        verifyNoInteractions(agentMemoryClient);
    }

    @Test
    @DisplayName("payload 非法：直接 DEAD（重试不会修复），不调用 Agent")
    void invalidPayloadMarksDead() {
        OutboxEvent broken = event(MemoryEventType.MEMORY_UPSERT, 0, 8);
        broken.setPayload("{}");
        when(outboxEventMapper.selectClaimable(any(), eq(BATCH_SIZE)))
                .thenReturn(List.of(broken));
        when(outboxEventMapper.claim(anyLong(), anyString(), any())).thenReturn(1);
        when(outboxEventMapper.markDead(anyLong(), anyString(), any(), anyString()))
                .thenReturn(1);

        worker.relayOnce();

        verify(outboxEventMapper).markDead(eq(MEMORY_ID), anyString(), any(),
                contains("payload 非法"));
        verifyNoInteractions(agentMemoryClient);
    }

    @Test
    @DisplayName("已关闭 Relay：不查询、不投递")
    void disabledRelayDoesNothing() {
        MemoryOutboxProperties disabled = new MemoryOutboxProperties(
                false, 2000L, 10000L, BATCH_SIZE, 1000, 120L, 8,
                2000L, 300_000L, 0.2, 86_400_000L, 60_000L, 1000);
        OutboxRelayWorker off = new OutboxRelayWorker(
                outboxEventMapper, agentMemoryClient, disabled);

        off.relay();

        verifyNoInteractions(outboxEventMapper, agentMemoryClient);
    }

    // ===================== 退避公式 =====================

    @Test
    @DisplayName("指数退避：2s 起步按 2 的幂增长，并封顶 max")
    void baseBackoffDoublesAndCaps() {
        assertThat(OutboxRelayWorker.baseBackoffMillis(1, 2000, 300_000)).isEqualTo(2_000);
        assertThat(OutboxRelayWorker.baseBackoffMillis(2, 2000, 300_000)).isEqualTo(4_000);
        assertThat(OutboxRelayWorker.baseBackoffMillis(3, 2000, 300_000)).isEqualTo(8_000);
        assertThat(OutboxRelayWorker.baseBackoffMillis(8, 2000, 300_000)).isEqualTo(256_000);
        assertThat(OutboxRelayWorker.baseBackoffMillis(9, 2000, 300_000)).isEqualTo(300_000);
        assertThat(OutboxRelayWorker.baseBackoffMillis(40, 2000, 300_000)).isEqualTo(300_000);
    }

    @Test
    @DisplayName("抖动：±20% 边界正确，ratio=0 时保持原值")
    void jitterStaysWithinRatio() {
        assertThat(OutboxRelayWorker.withJitter(1000, 0.2, 0.0)).isEqualTo(800);
        assertThat(OutboxRelayWorker.withJitter(1000, 0.2, 0.5)).isEqualTo(1000);
        assertThat(OutboxRelayWorker.withJitter(1000, 0.2, 1.0)).isEqualTo(1200);
        assertThat(OutboxRelayWorker.withJitter(1000, 0.0, 0.9)).isEqualTo(1000);
        assertThat(OutboxRelayWorker.withJitter(0, 0.2, 0.9)).isZero();
    }

    // ===================== helpers =====================

    private static OutboxEvent event(MemoryEventType type, int attemptCount, int maxAttempts) {
        MemoryEventPayload payload = new MemoryEventPayload(
                MEMORY_ID, USER_ID, "content", Hashing.sha256Hex("content"), 3,
                "SPACE", "SPACE:9", 9L, null, "LEARNING",
                type == MemoryEventType.MEMORY_UPSERT ? "ACTIVE" : "ARCHIVED",
                0.5f, null);
        OutboxEvent event = new OutboxEvent();
        event.setId(MEMORY_ID);
        event.setEventType(type.name());
        event.setAggregateType(OutboxRelayWorker.AGGREGATE_TYPE);
        event.setAggregateId(MEMORY_ID);
        event.setPayload(JsonUtil.toJson(payload));
        event.setStatus(OutboxEventStatus.PENDING);
        event.setAttemptCount(attemptCount);
        event.setMaxAttempts(maxAttempts);
        return event;
    }
}
