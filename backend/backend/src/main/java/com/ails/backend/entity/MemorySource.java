package com.ails.backend.entity;

import com.ails.backend.enums.MemorySourceType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Memory 的来源（memory_source）。
 * <p>
 * 删除 Memory 不删除原始 Evidence —— 这里只是指向来源的引用。
 */
@Data
@TableName("memory_source")
public class MemorySource {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long memoryId;

    private MemorySourceType sourceType;

    private Long sourceId;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
