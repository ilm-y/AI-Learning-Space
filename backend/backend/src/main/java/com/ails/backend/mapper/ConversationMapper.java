package com.ails.backend.mapper;

import com.ails.backend.entity.Conversation;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

public interface ConversationMapper extends BaseMapper<Conversation> {

    /**
     * 原子分配对话内的下一个消息序号。
     * <p>
     * 只锁 conversation 的主键行：同一对话的写入被串行化，不同对话各锁各的行，
     * 不会像 message 索引上的范围锁那样波及相邻对话。返回 0 表示对话不存在或已软删。
     */
    @Update("UPDATE conversation SET last_message_seq = last_message_seq + 1 "
            + "WHERE id = #{id} AND deleted_at IS NULL")
    int incrementLastMessageSeq(@Param("id") Long id);

    /**
     * 读回刚分配的序号。必须和 {@link #incrementLastMessageSeq} 在同一个事务里；
     * 读的是本事务自己刚写入的值，不受 REPEATABLE READ 快照影响。
     */
    @Select("SELECT last_message_seq FROM conversation WHERE id = #{id} AND deleted_at IS NULL")
    Integer selectLastMessageSeq(@Param("id") Long id);
}
