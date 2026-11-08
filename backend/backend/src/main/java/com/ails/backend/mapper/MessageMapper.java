package com.ails.backend.mapper;

import com.ails.backend.entity.Message;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

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
}
