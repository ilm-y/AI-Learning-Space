package com.ails.backend.service;

import com.ails.backend.config.MemoryOutboxProperties;
import com.ails.backend.dto.memory.agent.MemoryEventPayload;
import com.ails.backend.entity.Memory;
import com.ails.backend.entity.OutboxEvent;
import com.ails.backend.enums.MemoryEventType;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
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

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

/**
 * Outbox 发布端契约：事件类型、显式身份字段与 PENDING 初态。
 */
@ExtendWith(MockitoExtension.class)
class MemoryOutboxPublisherTest {

    private static final long MEMORY_ID = 42L;
    private static final long USER_ID = 7L;
    private static final String CONTENT = "用户偏好用递归解释分治";

    @Mock
    private OutboxEventMapper outboxEventMapper;

    private MemoryOutboxPublisher publisher;

    @BeforeEach
    void setUp() {
        publisher = new MemoryOutboxPublisher(outboxEventMapper,
                new MemoryOutboxProperties(true, 2000L, 10000L, 100, 1000, 120L, 8,
                        2000L, 300_000L, 0.2, 86_400_000L, 60_000L, 1000));
    }

    @Test
    @DisplayName("UPSERT：payload 自包含 userId / scopeType / contentHash，状态 PENDING")
    void publishUpsertCarriesExplicitIdentity() {
        publisher.publishUpsert(memory(MemoryStatus.ACTIVE));

        OutboxEvent event = captured();
        assertThat(event.getEventType()).isEqualTo(MemoryEventType.MEMORY_UPSERT.name());
        assertThat(event.getAggregateType()).isEqualTo("MEMORY");
        assertThat(event.getAggregateId()).isEqualTo(MEMORY_ID);
        assertThat(event.getStatus()).isEqualTo(OutboxEventStatus.PENDING);
        assertThat(event.getAttemptCount()).isZero();
        assertThat(event.getMaxAttempts()).isEqualTo(8);
        assertThat(event.getNextAttemptAt()).isNotNull();

        MemoryEventPayload payload = JsonUtil.fromJson(event.getPayload(), MemoryEventPayload.class);
        // 显式传递，不允许 Relay 从库内状态反推。
        assertThat(payload.userId()).isEqualTo(USER_ID);
        assertThat(payload.scopeType()).isEqualTo("SPACE");
        assertThat(payload.scopeKey()).isEqualTo("SPACE:9");
        assertThat(payload.memoryId()).isEqualTo(MEMORY_ID);
        assertThat(payload.content()).isEqualTo(CONTENT);
        assertThat(payload.contentHash()).isEqualTo(Hashing.sha256Hex(CONTENT));
        assertThat(payload.memoryVersion()).isEqualTo(3);
        assertThat(payload.status()).isEqualTo("ACTIVE");
    }

    @Test
    @DisplayName("DELETE：事件类型为 MEMORY_DELETE，payload 保留归档后状态")
    void publishDeleteUsesDeleteEventType() {
        publisher.publishDelete(memory(MemoryStatus.ARCHIVED));

        OutboxEvent event = captured();
        assertThat(event.getEventType()).isEqualTo(MemoryEventType.MEMORY_DELETE.name());
        MemoryEventPayload payload = JsonUtil.fromJson(event.getPayload(), MemoryEventPayload.class);
        assertThat(payload.status()).isEqualTo("ARCHIVED");
        assertThat(payload.userId()).isEqualTo(USER_ID);
        assertThat(payload.scopeType()).isEqualTo("SPACE");
    }

    @Test
    @DisplayName("未落库的 Memory（无 id）：拒绝发布，避免产生无法关联的事件")
    void rejectsMemoryWithoutId() {
        Memory memory = memory(MemoryStatus.ACTIVE);
        memory.setId(null);

        assertThatThrownBy(() -> publisher.publishUpsert(memory))
                .isInstanceOf(IllegalArgumentException.class);
        verify(outboxEventMapper, never()).insert(any(OutboxEvent.class));
    }

    private OutboxEvent captured() {
        ArgumentCaptor<OutboxEvent> captor = ArgumentCaptor.forClass(OutboxEvent.class);
        verify(outboxEventMapper).insert(captor.capture());
        return captor.getValue();
    }

    private static Memory memory(MemoryStatus status) {
        Memory memory = new Memory();
        memory.setId(MEMORY_ID);
        memory.setUserId(USER_ID);
        memory.setLearningSpaceId(9L);
        memory.setScopeType(MemoryScopeType.SPACE);
        memory.setScopeKey("SPACE:9");
        memory.setMemoryKey("teaching-style");
        memory.setMemoryType(MemoryType.LEARNING);
        memory.setContent(CONTENT);
        memory.setStatus(status);
        memory.setImportance(0.8f);
        memory.setVersion(3);
        return memory;
    }
}
