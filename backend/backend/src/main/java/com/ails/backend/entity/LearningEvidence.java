package com.ails.backend.entity;

import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceFollowUpStatus;
import com.ails.backend.enums.EvidenceType;
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
 * 去重口径由 {@code dedupeKey} 唯一决定，见 {@link EvidenceService}：
 * 同一个 user × concept × 证据类型 × 主来源 × 同一条观察，<b>无论由哪一次 run 产生</b>，
 * 都只算一条逻辑 Evidence。{@code firstSeenRunId} / {@code lastSeenRunId} 因此只是溯源元数据，
 * 不参与去重。
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

    /** 这条证据是怎么来的（用户解释 / 观察 / 测试 / 纠正 / 批注信号） */
    private EvidenceType evidenceType;

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

    /**
     * 首次看到本条观测的 run。
     * <p>
     * 注意它<b>不参与去重</b> —— 去重完全由 {@code dedupeKey} 决定，这两个 runId
     * 只回答「这条证据是哪次分析看到的、最近一次又是谁看到的」。
     */
    private Long firstSeenRunId;

    private Long lastSeenRunId;

    /** 分析器版本，用于回溯 */
    private String analyzerVersion;

    /**
     * 去重指纹（SHA-256 十六进制，64 位）。
     * <p>
     * 值 = SHA256(user_id + concept_id + evidence_type + 主来源 + 归一化 observation)。
     * 数据库上有 UNIQUE(dedupe_key)，是「重复分析不产生重复证据」的最终兜底 ——
     * 应用层查重只是省一次写入，不是保证。
     */
    private String dedupeKey;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
