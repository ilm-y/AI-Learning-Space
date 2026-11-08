package com.ails.backend.dto.concept;

import com.ails.backend.enums.RelationType;
import lombok.Data;

/**
 * concept_relation 的一次受控查询结果（Mapper 内部使用）。
 * <p>
 * <b>方向约定：</b>{@code direction} 是相对“被查询的概念”而言的：
 * <ul>
 *   <li>{@code OUTGOING}：被查询概念是 source，关联概念是 target；</li>
 *   <li>{@code INCOMING}：被查询概念是 target，关联概念是 source。</li>
 * </ul>
 * 对 PREREQUISITE 而言，{@code INCOMING} 表示关联概念是当前概念的<b>前置</b>
 * （即 source 是 target 的前置）。该假设由
 * {@code ConceptRelationServiceTest.prerequisiteDirectionIsSourceToTarget()} 固定。
 */
@Data
public class ConceptRelationRow {

    /** 相对于被查询概念的另一端概念 id */
    private Long relatedConceptId;

    private RelationType relationType;

    /** 关系强度 0~1，可能为 NULL */
    private Double weight;

    /** INCOMING / OUTGOING，相对于被查询概念 */
    private String direction;
}
