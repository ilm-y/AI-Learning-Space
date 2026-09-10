package com.ails.backend.entity;

import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceFollowUpStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 证据链（learning_evidence）。
 * <p>
 * 对用户认知判断的原子证据，<b>append-only</b>：新 Evidence 不删除也不覆盖旧 Evidence，
 * 由 {@link EvidenceSource} 指回真实的 Message / Highlight / TestAnswer。
 * <p>
 * 本表不提供删除入口 —— 学习历史必须长期可追溯。
 * <p>
 * 去重口径：不是「一个 source 只能产生一条 Evidence」（同一个 Message 本就可以为不同
 * Concept 各产生一条），而是「同一次 analysis run / 同一个 analyzer version 不重复写入
 * 相同 Evidence」。因此带 agentRunId / analyzerVersion / observationHash 三列。
 */
@Data
@TableName("learning_evidence")
public class LearningEvidence {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long conceptId;

    /** 关联的理解快照，由 getOrCreateUnderstanding 保证存在 */
    private Long understandingId;

    /** Agent 观察到的内容 */
    private String observation;

    private EvidenceCorrectness correctness;

    /** 推理质量 0~1，内部分析信号，不直接展示给用户 */
    private Float reasoningQuality;

    /** 针对性 0~1，内部分析信号 */
    private Float specificity;

    /** AI 对这条观察的把握度 0~1 */
    private Float aiConfidence;

    private Boolean needFollowUp;

    private EvidenceFollowUpStatus followUpStatus;

    /** 需要跟进的后续对话 */
    private Long followUpConversationId;

    /** 产生本条 Evidence 的 analysis run，用于溯源与去重 */
    private Long agentRunId;

    /** 分析器版本，用于去重与回溯 */
    private String analyzerVersion;

    /** observation 归一化后的 hash，用于去重 */
    private String observationHash;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
