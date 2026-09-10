package com.ails.backend.entity;

import com.ails.backend.enums.ConversationStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
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

    /** AI 自动总结的标题，可空 */
    private String title;

    private ConversationStatus status;

    /** 最后消息时间 */
    private LocalDateTime lastMessageAt;

    @TableLogic(value = "null", delval = "now()")
    private LocalDateTime deletedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
