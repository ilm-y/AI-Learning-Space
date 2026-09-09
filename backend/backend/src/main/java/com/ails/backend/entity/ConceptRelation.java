package com.ails.backend.entity;

import com.ails.backend.enums.RelationType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 概念关系表（concept_relation）
 */
@Data
@TableName("concept_relation")
public class ConceptRelation {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 源知识点 */
    private Long sourceConceptId;

    /** 目标知识点 */
    private Long targetConceptId;

    private RelationType relationType;

    /** 关系强度 0~1 */
    private Double weight;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
