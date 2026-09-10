package com.ails.backend.entity;

import com.ails.backend.enums.SpaceVisibility;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 学习空间（learning_space）。
 * <p>
 * 软删：空间下面是对话、Concept、Understanding、Evidence，
 * 物理删除会顺着级联把长期学习历史一起炸掉。
 */
@Data
@TableName("learning_space")
public class LearningSpace {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 所属用户 */
    private Long userId;

    private String name;

    private String description;

    private SpaceVisibility visibility;

    @TableLogic(value = "null", delval = "now()")
    private LocalDateTime deletedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
