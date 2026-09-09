package com.ails.backend.entity;

import com.ails.backend.enums.SpaceVisibility;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 学习空间表（learning_space）
 */
@Data
@TableName("learning_space")
public class LearningSpace {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 所有者 user.id */
    private Long userId;

    /** 如 "操作系统内核" */
    private String name;

    /** 空间描述 */
    private String description;

    /** 默认 PRIVATE */
    private SpaceVisibility visibility;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
