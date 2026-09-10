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
 * 批注表（highlight）。
 * <p>
 * start_offset / end_offset 必须保存，否则以后无法可靠地定位原始消息中的选区。
 */
@Data
@TableName("highlight")
public class Highlight {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 冗余 user.id，方便快速查询 */
    private Long userId;

    /** 标记在哪条消息上 */
    private Long messageId;

    /** 选中的原文片段 */
    private String selectedText;

    /** 选区起点 */
    private Integer startOffset;

    /** 选区终点 */
    private Integer endOffset;

    private HighlightType markType;

    /** 用户写的批注 */
    private String note;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
