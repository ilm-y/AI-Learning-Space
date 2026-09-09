package com.ails.backend.entity;

import com.ails.backend.enums.HighlightType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 高亮标记表（highlight）
 */
@Data
@TableName("highlight")
public class Highlight {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 冗余 user.id，方便快速查询 */
    private Long userId;

    /** 标记在哪条 AI 回复上 */
    private Long messageId;

    /** 选中的原文片段 */
    private String selectedText;

    private HighlightType type;

    /** 用户写的批注 */
    private String note;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
