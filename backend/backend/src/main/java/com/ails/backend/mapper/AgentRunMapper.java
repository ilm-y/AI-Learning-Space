package com.ails.backend.mapper;

import com.ails.backend.entity.AgentRun;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.util.List;

public interface AgentRunMapper extends BaseMapper<AgentRun> {

    /**
     * 补偿扫描：找出创建时间早于 cutoff、仍停在非终态的分析任务。
     * <p>
     * 只取 ANALYSIS：CHAT / TEST run 的生命周期由各自请求线程负责，不能被这里误杀。
     * 用 SQL 过滤而不是先查全表再筛选，避免把大量历史 run 拉进内存。
     */
    @Select("""
            SELECT *
              FROM agent_run
             WHERE run_type = 'ANALYSIS'
               AND status IN ('PENDING', 'RUNNING')
               AND created_at < #{cutoff}
             ORDER BY created_at ASC
             LIMIT #{limit}
            """)
    List<AgentRun> selectStaleAnalysisRuns(@Param("cutoff") LocalDateTime cutoff,
                                           @Param("limit") int limit);

    /**
     * 把一条超时任务标记为 {@code STALE}（可能遗留），而不是 FAILED。
     * <p>
     * 单凭 {@code created_at} 无法证明任务没有执行：它可能还在队列里，也可能正在调用模型。
     * STALE 只是可观测标记，结果消费者仍允许把它抢占为 RUNNING，晚到的合法结果不会被拒绝。
     * 不写 {@code finished_at} / {@code duration_ms} —— 任务并没有被确认结束。
     * <p>
     * 条件里同时保留 {@code status IN ('PENDING','RUNNING')} 与 {@code created_at < cutoff}：
     * 如果结果消费者刚刚抢占/完成，这里会更新 0 行，不会覆盖真正的终态。
     *
     * @return 1 表示确实由补偿扫描标记为 STALE；0 表示任务已经不在待补偿状态
     */
    @Update("""
            UPDATE agent_run
               SET status = 'STALE',
                   error_message = #{error}
             WHERE id = #{id}
               AND status IN ('PENDING', 'RUNNING')
               AND created_at < #{cutoff}
            """)
    int markAnalysisRunStale(@Param("id") Long id,
                             @Param("cutoff") LocalDateTime cutoff,
                             @Param("error") String error);
}
