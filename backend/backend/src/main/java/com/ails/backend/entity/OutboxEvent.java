package com.ails.backend.entity;

import com.ails.backend.enums.OutboxEventStatus;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Transactional Outbox 事件（{@code outbox_event}）。
 *
 * <p><b>payload 是自包含快照</b>：Relay 处理时不再回表读取 Memory，避免事件语义
 * 随之后写入的内容漂移，也方便人工重放。</p>
 *
 * <p>{@code payload} 用 String 承载 JSON 列：项目其余 JSON 列（如
 * {@code learning_understanding.misconceptions}）同样以 String 读写，MySQL 会校验
 * JSON 合法性。不在这里做实体级 JSON 反序列化，因为 Relay 需要把非法 payload
 * 归类为永久失败，而不是在读取阶段就抛异常。</p>
 */
@Data
@TableName("outbox_event")
public class OutboxEvent {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** {@link com.ails.backend.enums.MemoryEventType} 的名称。 */
    private String eventType;

    private String aggregateType;

    /** 当前只有 MEMORY，值为 memory.id。 */
    private Long aggregateId;

    private String payload;

    private OutboxEventStatus status;

    private Integer attemptCount;

    private Integer maxAttempts;

    /** 下一次允许被领取的时间；指数退避写在这里。 */
    private LocalDateTime nextAttemptAt;

    private String lockedBy;

    private LocalDateTime lockedAt;

    /** 只存错误摘要，最多 500 字符；不存堆栈与正文。 */
    private String lastError;

    /** 仅回填脚本使用；正常写入为 NULL。 */
    private String dedupKey;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;

    private LocalDateTime completedAt;
}
