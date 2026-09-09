package com.ails.backend.entity;

import com.ails.backend.enums.PlanStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 学习计划表（learning_plan，长期）
 */
@Data
@TableName("learning_plan")
public class LearningPlan {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long learningSpaceId;

    /** 如 "7天掌握Kafka Consumer" */
    private String title;

    private String description;

    /** 学习目标描述 */
    private String goal;

    private PlanStatus status;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
