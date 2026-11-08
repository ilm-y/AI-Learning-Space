package com.ails.backend.mapper;

import com.ails.backend.entity.Message;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.Collection;
import java.util.List;

/**
 * 消息。
 * <p>
 * 对话内序号由 {@code conversation.last_message_seq} 原子分配（见
 * {@link ConversationMapper#incrementLastMessageSeq}），不再需要在 message 索引上做
 * 锁定范围读。
 * <p>
 * 注意：{@code @TableLogic} 只作用于 MyBatis-Plus 自带方法拼出来的语句；
 * 序号分配也不再依赖「把软删行算进 MAX」这个细节。手写 SQL 必须自己带
 * {@code deleted_at IS NULL}。
 */
public interface MessageMapper extends BaseMapper<Message> {

    /**
     * 按 {@code (conversation_id, client_request_id)} 查幂等键对应的 USER 消息。
     * <p>
     * 数据库上有对应的唯一键；这里仍用 {@code LIMIT 1} + {@code ORDER BY id ASC}，
     * 是为了让历史脏数据（唯一键上线前写入的重复行，理论上 client_request_id 全为 NULL）
     * 不会让查询报错 —— 正常数据最多一行。
     */
    @Select("SELECT * FROM message "
            + "WHERE conversation_id = #{conversationId} "
            + "  AND client_request_id = #{clientRequestId} "
            + "  AND deleted_at IS NULL "
            + "ORDER BY id ASC LIMIT 1")
    Message selectByConversationAndClientRequestId(@Param("conversationId") Long conversationId,
                                                   @Param("clientRequestId") String clientRequestId);

    /**
     * 取某对话的历史 TEXT 消息，并<b>在 SQL 内校验对话归属</b>。
     * <p>
     * {@code message} 表没有 {@code user_id}，归属只能由 {@code conversation} 决定。
     * 与 MyBatis-Plus 的 Wrapper 查询不同，手写 SQL 不会自动追加 {@code @TableLogic}，
     * 因此这里显式过滤双方软删标记。{@code userId} 与对话不匹配时查询自然返回空列表，
     * 不需要调用方先做一次归属查询 —— 这也是本方法存在的目的：让任何新增调用方
     * 都不可能只凭 {@code conversationId} 读到别人的历史。
     *
     * @param userId           当前用户；必须与 conversation.user_id 一致
     * @param conversationId   对话 id
     * @param excludeMessageIds 需要排除的本轮消息，可为空
     * @param limit            最多返回条数
     */
    @Select("""
            <script>
            SELECT m.*
            FROM message m
            JOIN conversation c ON c.id = m.conversation_id
            WHERE m.conversation_id = #{conversationId}
              AND c.user_id = #{userId}
              AND m.message_type = 'TEXT'
              AND m.deleted_at IS NULL
              AND c.deleted_at IS NULL
            <if test="excludeMessageIds != null and excludeMessageIds.size() > 0">
              AND m.id NOT IN
              <foreach collection="excludeMessageIds" item="excludedId" open="(" separator="," close=")">
                  #{excludedId}
              </foreach>
            </if>
            ORDER BY m.sequence_no DESC, m.id DESC
            LIMIT #{limit}
            </script>
            """)
    List<Message> selectHistoryForUser(
            @Param("userId") Long userId,
            @Param("conversationId") Long conversationId,
            @Param("excludeMessageIds") Collection<Long> excludeMessageIds,
            @Param("limit") int limit);
}
