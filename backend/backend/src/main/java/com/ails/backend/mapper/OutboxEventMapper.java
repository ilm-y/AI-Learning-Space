package com.ails.backend.mapper;

import com.ails.backend.entity.OutboxEvent;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.util.List;

/**
 * {@code outbox_event} 的领取与状态迁移 SQL。
 *
 * <h3>为什么领取要带 head-of-line 条件</h3>
 * 同一 {@code memory_id} 的事件必须严格有序：若事件 5（内容 A）失败进入退避、
 * 事件 6（内容 B）却先成功，事件 5 之后重试会把索引写回 A，而 MySQL 里已经是 B。
 * {@link #selectClaimable} 的 {@code NOT EXISTS} 子查询要求「同聚合没有更早的
 * 未终态事件」，从根上消除这个窗口。
 *
 * <h3>为什么所有回写都带 {@code locked_by}</h3>
 * 租约过期后事件可能被另一个实例重新领取。如果旧实例此时才完成/失败，
 * 无条件回写会覆盖新实例的结果（fencing）。所以完成、重试、死信都要求
 * {@code status = 'PROCESSING' AND locked_by = #{workerId}}，更新 0 行即表示
 * 本次租约已经失效，直接放弃。
 */
public interface OutboxEventMapper extends BaseMapper<OutboxEvent> {

    /**
     * 选取「到期、且同聚合没有更早未完成事件」的候选。
     *
     * <p>{@code ORDER BY id} 保证单轮内按事件写入顺序处理；跨聚合顺序无所谓。</p>
     */
    @Select("""
            SELECT e.*
            FROM outbox_event e
            WHERE e.status = 'PENDING'
              AND e.next_attempt_at <= #{now}
              AND NOT EXISTS (
                  SELECT 1
                  FROM outbox_event p
                  WHERE p.aggregate_type = e.aggregate_type
                    AND p.aggregate_id   = e.aggregate_id
                    AND p.id             < e.id
                    AND p.status IN ('PENDING', 'PROCESSING')
              )
            ORDER BY e.id
            LIMIT #{batchSize}
            """)
    List<OutboxEvent> selectClaimable(@Param("now") LocalDateTime now,
                                      @Param("batchSize") int batchSize);

    /**
     * 条件领取：只有仍是 PENDING 且到期的行才能被抢到。
     *
     * @return 1 表示领取成功；0 表示被其他实例抢先或已不再到期
     */
    @Update("""
            UPDATE outbox_event
            SET status = 'PROCESSING',
                locked_by = #{workerId},
                locked_at = #{now},
                attempt_count = attempt_count + 1
            WHERE id = #{id}
              AND status = 'PENDING'
              AND next_attempt_at <= #{now}
            """)
    int claim(@Param("id") Long id,
              @Param("workerId") String workerId,
              @Param("now") LocalDateTime now);

    /** 成功：PROCESSING → DONE，释放租约。 */
    @Update("""
            UPDATE outbox_event
            SET status = 'DONE',
                locked_by = NULL,
                locked_at = NULL,
                completed_at = #{now},
                last_error = NULL
            WHERE id = #{id}
              AND status = 'PROCESSING'
              AND locked_by = #{workerId}
            """)
    int markDone(@Param("id") Long id,
                 @Param("workerId") String workerId,
                 @Param("now") LocalDateTime now);

    /** 可重试失败：PROCESSING → PENDING，写入下一次允许领取的时间与错误摘要。 */
    @Update("""
            UPDATE outbox_event
            SET status = 'PENDING',
                locked_by = NULL,
                locked_at = NULL,
                next_attempt_at = #{nextAttemptAt},
                last_error = #{lastError}
            WHERE id = #{id}
              AND status = 'PROCESSING'
              AND locked_by = #{workerId}
            """)
    int markPending(@Param("id") Long id,
                    @Param("workerId") String workerId,
                    @Param("nextAttemptAt") LocalDateTime nextAttemptAt,
                    @Param("lastError") String lastError);

    /** 永久失败/重试耗尽：PROCESSING → DEAD，保留 payload 供人工重放。 */
    @Update("""
            UPDATE outbox_event
            SET status = 'DEAD',
                locked_by = NULL,
                locked_at = NULL,
                completed_at = #{now},
                last_error = #{lastError}
            WHERE id = #{id}
              AND status = 'PROCESSING'
              AND locked_by = #{workerId}
            """)
    int markDead(@Param("id") Long id,
                 @Param("workerId") String workerId,
                 @Param("now") LocalDateTime now,
                 @Param("lastError") String lastError);

    /**
     * 回收过期租约：PROCESSING 超时 → PENDING。
     *
     * <p>{@code attempt_count} 保持已累加值，避免「崩溃 → 立即重试 → 再崩溃」无限循环。</p>
     */
    @Update("""
            UPDATE outbox_event
            SET status = 'PENDING',
                locked_by = NULL,
                locked_at = NULL,
                next_attempt_at = #{now}
            WHERE status = 'PROCESSING'
              AND locked_at < #{cutoff}
            """)
    int recoverExpiredLeases(@Param("cutoff") LocalDateTime cutoff,
                             @Param("now") LocalDateTime now);

    /** 清理 7 天前完成的事件；按批执行，避免长事务。 */
    @Update("""
            DELETE FROM outbox_event
            WHERE status = 'DONE'
              AND completed_at < #{cutoff}
            LIMIT #{batchSize}
            """)
    int deleteDoneBefore(@Param("cutoff") LocalDateTime cutoff,
                         @Param("batchSize") int batchSize);
}
