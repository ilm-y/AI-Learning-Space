package com.ails.backend.entity;

import com.ails.backend.enums.MessageRole;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 消息表（message）
 */
@Data
@TableName("message")
public class Message {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long conversationId;

    private MessageRole role;

    /** Markdown 格式内容 */
    private String content;

    /** 粗略 token 计数 */
    private Integer tokens;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
