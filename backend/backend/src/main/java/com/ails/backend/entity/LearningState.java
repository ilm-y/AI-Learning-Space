package com.ails.backend.entity;

import com.ails.backend.enums.LearningStateStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 学习状态表（learning_state，用户 × 知识点）。
 * <p>
 * 规格原设计为复合主键 (user_id, concept_id)，MyBatis-Plus 不支持复合主键，
 * 故增加自增代理主键 id，并保留 UNIQUE(user_id, concept_id)。
 */
@Data
@TableName("learning_state")
public class LearningState {

    /** 代理主键（见类注释） */
    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long conceptId;

    /** 掌握度 0~1，默认 0.0 */
    private Double mastery;

    /** 系统置信度 0~1，低于 0.65 会触发验证 */
    private Double confidence;

    /** 稳定性，多次表现一致则升高 */
    private Double stability;

    private LearningStateStatus status;

    /** 最近一次获得证据的时间 */
    private LocalDateTime lastEvidenceAt;

    /** 下次复习时间（由 Review Agent 计算） */
    private LocalDateTime nextReviewAt;

    /** 优先级越高越该复习 */
    private Integer reviewPriority;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
