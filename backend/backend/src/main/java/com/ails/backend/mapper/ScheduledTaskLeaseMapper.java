package com.ails.backend.mapper;

import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.time.LocalDateTime;

/**
 * 多实例定时任务互斥租约。
 *
 * <p>只有一个 {@code owner} 能在租约到期前执行任务。任务结束主动释放；进程崩溃时
 * 租约在 {@code expires_at} 后自动可被抢占，不需要额外清理进程。</p>
 *
 * <p>为什么用表而不是 {@code GET_LOCK}：{@code GET_LOCK} 绑定在单个数据库连接上，
 * Spring 连接池无法保证加锁/解锁走同一连接；而本任务会在执行期间做大量外部 HTTP
 * 调用，不应长时间占用一个事务连接。</p>
 */
public interface ScheduledTaskLeaseMapper {

    /**
     * 尝试获取租约。已存在且未过期时不变更任何行，返回 0；新建或抢占过期租约返回 >0。
     * 调用方仍需 {@link #selectOwner} 复核 owner，避免极小概率的竞争。
     */
    @Insert("""
            INSERT INTO scheduled_task_lease (task_name, owner, expires_at)
            VALUES (#{taskName}, #{owner}, #{expiresAt})
            ON DUPLICATE KEY UPDATE
                owner = IF(expires_at < #{now}, VALUES(owner), owner),
                expires_at = IF(expires_at < #{now}, VALUES(expires_at), expires_at)
            """)
    int tryAcquire(@Param("taskName") String taskName,
                   @Param("owner") String owner,
                   @Param("now") LocalDateTime now,
                   @Param("expiresAt") LocalDateTime expiresAt);

    @Select("SELECT owner FROM scheduled_task_lease WHERE task_name = #{taskName}")
    String selectOwner(@Param("taskName") String taskName);

    @Delete("""
            DELETE FROM scheduled_task_lease
            WHERE task_name = #{taskName} AND owner = #{owner}
            """)
    int release(@Param("taskName") String taskName, @Param("owner") String owner);
}
