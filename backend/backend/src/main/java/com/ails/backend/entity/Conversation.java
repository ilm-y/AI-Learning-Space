package com.ails.backend.entity;

import com.ails.backend.enums.ConversationStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.FieldStrategy;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 对话表（conversation）。
 * <p>
 * 删除采用软删：对话本身是学习历史的容器，物理删除会连带毁掉证据链。
 */
@Data
@TableName("conversation")
public class Conversation {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long learningSpaceId;

    /**
     * 所属的学习活动。
     * <p>
     * <b>可以为 null，而且这是正常状态</b>，不是「数据没填全」：
     * <ul>
     *   <li>刚建出来还没说话；</li>
     *   <li>说了话，但没命中任何 Concept（闲聊、问一个全新的知识点）——
     *       {@code SessionResolver} 这时给出 NONE，刻意不创建 Session。</li>
     * </ul>
     * 一旦第一条有效学习消息出现，{@code SessionService} 会把它填上，之后不再改回去。
     */
    private Long sessionId;

    /** AI 自动总结的标题，可空 */
    private String title;

    private ConversationStatus status;

    /** 最后消息时间 */
    private LocalDateTime lastMessageAt;

    /**
     * 对话内最后分配的消息序号（0 = 尚未分配）。
     * <p>
     * 分配走 {@code ConversationMapper.incrementLastMessageSeq} 的原子自增，
     * 不读 {@code MAX(message.sequence_no)}：后者需要在 message 的
     * {@code uk_msg_conv_seq} 上做锁定范围读，空对话会锁住相邻对话之间的间隙，
     * 并发写不同对话时互相等待，实测出现 {@code Deadlock found when trying to get lock}。
     * <p>
     * <b>必须禁止 updateById 回写。</b> 实体是先读后改的，读到的值可能已经过期；
     * 若 updateById 把它一起写回，会把并发分配出去的序号覆盖掉。
     */
    @TableField(updateStrategy = FieldStrategy.NEVER)
    private Integer lastMessageSeq;

    @TableLogic(value = "null", delval = "now()")
    private LocalDateTime deletedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
