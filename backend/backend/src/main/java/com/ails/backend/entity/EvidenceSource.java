package com.ails.backend.entity;

import com.ails.backend.enums.EvidenceSourceType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Evidence 的真实来源（evidence_source）。
 * <p>
 * source_id 是多态引用，数据库无法保证它真的指向对应类型的对象，
 * 因此保留统一表 + UNIQUE(evidence_id, source_type, source_id)，由应用层校验真实对象存在。
 * 一条 Evidence 可以同时由 Message 与 Highlight 支撑，所以这里是 1:N。
 */
@Data
@TableName("evidence_source")
public class EvidenceSource {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long evidenceId;

    private EvidenceSourceType sourceType;

    private Long sourceId;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
