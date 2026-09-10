package com.ails.backend.entity;

import com.ails.backend.enums.UnderstandingStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 学习状态（learning_state）—— {@link LearningUnderstanding} 状态的物化投影。
 * <p>
 * 状态取值与 understanding.status 完全一致，由 LearningStateService 单向派生，
 * 不允许独立修改，避免出现「Understanding 说 PARTIAL、LearningState 说 DEMONSTRATED」
 * 这种不知道该信谁的分裂。
 * <p>
 * 规格原设计为复合主键 (user_id, concept_id)，MyBatis-Plus 不支持复合主键，
 * 故增加自增代理主键 id，并保留 UNIQUE(user_id, concept_id)。
 * <p>
 * 第一版不保存 mastery / confidence / stability。
 */
@Data
@TableName("learning_state")
public class LearningState {

    /** 代理主键（见类注释） */
    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long conceptId;

    private UnderstandingStatus status;

    /** 下次复习时间 */
    private LocalDateTime nextReviewAt;

    /** 优先级越高越该复习 */
    private Integer reviewPriority;

    /** 最近一次获得证据的时间 */
    private LocalDateTime lastEvidenceAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
