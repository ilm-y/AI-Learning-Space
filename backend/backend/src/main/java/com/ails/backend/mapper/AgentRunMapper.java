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
     * 按 {@code (conversation_id, client_request_id)} 查幂等键对应的 CHAT run。
     * <p>
     * 与 message 上的唯一键同源：USER 消息与 run 在同一个预创建事务里写入，
     * 因此正常数据最多一行。{@code LIMIT 1} 只是防御历史 / 手工数据。
     */
    @Select("SELECT * FROM agent_run "
            + "WHERE conversation_id = #{conversationId} "
            + "  AND client_request_id = #{clientRequestId} "
            + "ORDER BY id DESC LIMIT 1")
    AgentRun selectByConversationAndClientRequestId(@Param("conversationId") Long conversationId,
                                                    @Param("clientRequestId") String clientRequestId);

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

    /**
     * D4-4：按 policy_version / intent / 时间范围分页查询某空间内的 Manifest。
     * <p>
     * {@code agent_run} 没有 {@code space_id}，空间归属只能经 conversation 证明；
     * 这里同时带上 {@code r.user_id}、{@code c.learning_space_id} 与
     * {@code c.deleted_at IS NULL}，保证跨用户 / 跨空间 / 已删对话都查不到。
     * <p>
     * policy_version / intent 存在 JSON 列里，用 {@code JSON_EXTRACT} 过滤。
     * 这条查询没有为 JSON 路径建索引，仅用于开发排查，不适合高 QPS。
     */
    @Select("""
            <script>
            SELECT r.*
              FROM agent_run r
              JOIN conversation c ON c.id = r.conversation_id
             WHERE r.user_id = #{userId}
               AND c.learning_space_id = #{spaceId}
               AND c.deleted_at IS NULL
               AND r.context_manifest IS NOT NULL
              <if test="policyVersion != null and policyVersion != ''">
               AND JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.policy_version')) = #{policyVersion}
              </if>
              <if test="intent != null and intent != ''">
               AND JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.intent')) = #{intent}
              </if>
              <if test="from != null">
               AND r.created_at &gt;= #{from}
              </if>
              <if test="to != null">
               AND r.created_at &lt; #{to}
              </if>
             ORDER BY r.created_at DESC, r.id DESC
             LIMIT #{limit} OFFSET #{offset}
            </script>
            """)
    List<AgentRun> selectManifestPage(@Param("userId") Long userId,
                                      @Param("spaceId") Long spaceId,
                                      @Param("policyVersion") String policyVersion,
                                      @Param("intent") String intent,
                                      @Param("from") LocalDateTime from,
                                      @Param("to") LocalDateTime to,
                                      @Param("limit") int limit,
                                      @Param("offset") int offset);

    /** 与 {@link #selectManifestPage} 同条件的总数，供分页响应使用。 */
    @Select("""
            <script>
            SELECT COUNT(*)
              FROM agent_run r
              JOIN conversation c ON c.id = r.conversation_id
             WHERE r.user_id = #{userId}
               AND c.learning_space_id = #{spaceId}
               AND c.deleted_at IS NULL
               AND r.context_manifest IS NOT NULL
              <if test="policyVersion != null and policyVersion != ''">
               AND JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.policy_version')) = #{policyVersion}
              </if>
              <if test="intent != null and intent != ''">
               AND JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.intent')) = #{intent}
              </if>
              <if test="from != null">
               AND r.created_at &gt;= #{from}
              </if>
              <if test="to != null">
               AND r.created_at &lt; #{to}
              </if>
            </script>
            """)
    long countManifestPage(@Param("userId") Long userId,
                           @Param("spaceId") Long spaceId,
                           @Param("policyVersion") String policyVersion,
                           @Param("intent") String intent,
                           @Param("from") LocalDateTime from,
                           @Param("to") LocalDateTime to);
}
