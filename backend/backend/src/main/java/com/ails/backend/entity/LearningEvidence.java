package com.ails.backend.entity;

import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 证据链表（learning_evidence，可追溯、可解释）
 */
@Data
@TableName("learning_evidence")
public class LearningEvidence {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long conceptId;

    /** 来源：MESSAGE(聊天)/HIGHLIGHT(笔记)/VERIFICATION(主动验证)/IMPORTED(导入历史) */
    private EvidenceSourceType sourceType;

    /** 具体来源 ID（message.id / highlight.id / verification_answer.id） */
    private Long sourceId;

    /** Agent 观察到的内容 */
    private String observation;

    private EvidenceCorrectness correctness;

    /** 权重：普通对话 0.8，主动验证回答 1.2，导入历史 0.6 */
    private Double weight;

    /** Agent 对这条观察的把握度 0~1 */
    private Double aiConfidence;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
