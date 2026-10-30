package com.ails.backend.mapper;

import com.ails.backend.entity.Message;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;

/**
 * 消息。
 * <p>
 * 对话内序号由 {@code conversation.last_message_seq} 原子分配（见
 * {@link ConversationMapper#incrementLastMessageSeq}），不再需要在 message 索引上做
 * 锁定范围读，因此这里没有手写 SQL。
 * <p>
 * 注意：{@code @TableLogic} 只作用于 MyBatis-Plus 自带方法拼出来的语句；
 * 序号分配也不再依赖「把软删行算进 MAX」这个细节。
 */
public interface MessageMapper extends BaseMapper<Message> {
}
