package com.ails.backend.service;

import com.ails.backend.config.MemoryOutboxProperties;
import com.ails.backend.dto.memory.agent.MemoryEventPayload;
import com.ails.backend.entity.Memory;
import com.ails.backend.entity.OutboxEvent;
import com.ails.backend.enums.MemoryEventType;
import com.ails.backend.enums.OutboxEventStatus;
import com.ails.backend.mapper.OutboxEventMapper;
import com.ails.backend.utils.JsonUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;

/**
 * 在 Memory 写事务内追加 Outbox 事件。
 *
 * <p><b>必须由业务写事务调用，且不能加 {@code @Transactional(REQUIRES_NEW)}。</b>
 * 事件与 Memory 共用同一个 InnoDB 事务：业务失败时事件一起回滚，
 * 不会出现「记忆存在但永远不会被索引」或「事件指向不存在的记忆」。</p>
 *
 * <p>payload 序列化失败必须抛出，让业务写入一起回滚；吞掉异常会留下一条
 * 无任何痕迹的「永远不索引」记忆。</p>
 */
@Component
@RequiredArgsConstructor
public class MemoryOutboxPublisher {

    static final String AGGREGATE_TYPE = "MEMORY";

    private final OutboxEventMapper outboxEventMapper;
    private final MemoryOutboxProperties properties;

    public void publishUpsert(Memory memory) {
        enqueue(MemoryEventType.MEMORY_UPSERT, memory);
    }

    public void publishDelete(Memory memory) {
        enqueue(MemoryEventType.MEMORY_DELETE, memory);
    }

    private void enqueue(MemoryEventType type, Memory memory) {
        if (memory == null || memory.getId() == null) {
            throw new IllegalArgumentException("Outbox 事件必须关联已落库的 Memory");
        }
        OutboxEvent event = new OutboxEvent();
        event.setEventType(type.name());
        event.setAggregateType(AGGREGATE_TYPE);
        event.setAggregateId(memory.getId());
        // 显式写入 userId / scopeType：下游（Relay/Agent）不自行推导，避免
        // 处理时库内状态已变化导致事件语义漂移。
        event.setPayload(JsonUtil.toJson(MemoryEventPayload.from(memory)));
        event.setStatus(OutboxEventStatus.PENDING);
        event.setAttemptCount(0);
        event.setMaxAttempts(properties.maxAttempts());
        event.setNextAttemptAt(LocalDateTime.now());
        outboxEventMapper.insert(event);
    }
}
