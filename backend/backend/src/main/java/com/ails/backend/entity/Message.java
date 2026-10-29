package com.ails.backend.entity;

import com.ails.backend.enums.MessageRole;
import com.ails.backend.enums.MessageType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 消息表（message）。
 * <p>
 * sequence_no 是对话内顺序，靠「锁 conversation 行再取下一个序号」保证并发安全，
 * 而不是 SELECT MAX(sequence_no) + 1（并发会拿到同一个数字）。
 */
@Data
@TableName("message")
public class Message {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long conversationId;

    private MessageRole role;

    private MessageType messageType;

    /** Markdown 格式内容 */
    private String content;

    /** 粗略 token 计数 */
    private Integer tokens;

    /**
     * 助手回复是否因模型 Token 上限被截断。
     * <p>
     * 截断时内容仍保留展示，但这个标记让重新加载消息列表的客户端也能区分
     * 「部分内容」和「完整回答」。用户消息、历史消息始终为 false。
     */
    private Boolean truncated;

    /** 对话内顺序，从 1 开始 */
    private Integer sequenceNo;

    /** 软删标记：删除对话时保留学习历史，不做物理删除 */
    @TableLogic(value = "null", delval = "now()")
    private LocalDateTime deletedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
