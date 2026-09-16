package com.ails.backend.mapper;

import com.ails.backend.entity.Message;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * 消息。除了 MyBatis-Plus 自带方法，另有一条手写 SQL —— {@code @TableLogic} 只作用于
 * 自带方法拼出来的语句，手写 {@code @Select} 不会被追加 {@code deleted_at IS NULL}。
 */
public interface MessageMapper extends BaseMapper<Message> {

    /**
     * 取对话内当前最大的 sequence_no（<b>行锁读</b>，没有行时返回 null）。
     * <p>
     * 这条 SQL 同时解决两个必须一起解决的问题，缺一个都会在并发下炸：
     * <p>
     * <b>1. 必须用手写 SQL，不能用 {@code selectObjs("MAX(sequence_no)")}。</b>
     * 自带方法会被 {@code @TableLogic} 追加 {@code deleted_at IS NULL}，被软删的消息就不参与 MAX；
     * 而 {@code uk_msg_conv_seq(conversation_id, sequence_no)} 是普通唯一索引，<b>仍然算上软删行</b> ——
     * 一旦被软删的恰好是序号最大的那条，下一个序号会复用它、撞唯一键，该对话再也发不出消息。
     * 序号只要求「对话内唯一且递增」，不要求连续，所以把软删行算进来才是对的。
     * <p>
     * <b>2. 必须是 {@code FOR UPDATE} 锁定读，不能是普通 SELECT。</b> 这一条更隐蔽，也更容易写错。
     * 调用方在拿到序列号之前已经做过普通 SELECT（查对话、查学习空间），而 InnoDB 的一致性读视图
     * 是在事务里<b>第一条普通 SELECT</b> 时建立的、之后不再更新。于是「先普通读、再拿锁、再普通读 MAX」
     * 这个顺序下，MAX 读到的是<b>钉在旧时刻的快照</b>：两个事务各拿到同一把行锁、依次进入临界区，
     * 第二个却仍然看到对方插入之前的 MAX，算出同一个序号 → {@code Duplicate entry}。
     * 锁定读永远读最新已提交版本，且它建立不了快照，正好绕开这一点。
     * <p>
     * 两者叠起来的效果是：只要调用方已经持有该 conversation 行的写锁（见
     * {@code MessageService.nextSequenceNo}），这条查询返回的就是「锁内最新」的最大序号。
     * <p>
     * <b>不加聚合函数也是刻意的</b>：{@code MAX()} 配 {@code FOR UPDATE} 在 MySQL 里语义含糊，
     * 而 {@code ORDER BY ... DESC LIMIT 1 FOR UPDATE} 锁的是确定的那一行，行为清楚。
     */
    @Select("SELECT sequence_no FROM message WHERE conversation_id = #{conversationId} "
            + "ORDER BY sequence_no DESC LIMIT 1 FOR UPDATE")
    Integer selectMaxSequenceNoForUpdate(@Param("conversationId") Long conversationId);
}
