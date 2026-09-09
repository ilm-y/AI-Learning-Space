package com.ails.backend.entity;

import com.ails.backend.enums.SessionStatus;
import com.ails.backend.enums.SessionType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 学习课程表（learning_session，短期，一次学习活动）
 */
@Data
@TableName("learning_session")
public class LearningSession {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long learningSpaceId;

    /** learning_plan.id，可空（可独立存在） */
    private Long learningPlanId;

    /** 如 "TLB 专项复习" */
    private String title;

    private SessionType sessionType;

    private SessionStatus status;

    private LocalDateTime startedAt;

    private LocalDateTime finishedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
