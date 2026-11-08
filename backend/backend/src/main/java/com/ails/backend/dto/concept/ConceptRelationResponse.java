package com.ails.backend.dto.concept;

import com.ails.backend.enums.RelationType;

/**
 * 知识点关系响应。
 * <p>
 * {@code direction} 相对被查询概念：{@code INCOMING} 表示关联概念是 source、
 * 被查询概念是 target；{@code OUTGOING} 反之。Agent 只把
 * PREREQUISITE + INCOMING 当作“当前概念的前置”，RELATED 视为双向相关。
 *
 * @param conceptId    关联概念 id（不是被查询概念）
 * @param conceptName  关联概念展示名（displayName 优先，否则 canonicalName）
 * @param relationType 关系类型
 * @param weight       关系强度 0~1，可能为 null
 * @param direction    INCOMING / OUTGOING，相对于被查询概念
 */
public record ConceptRelationResponse(
        Long conceptId,
        String conceptName,
        RelationType relationType,
        Double weight,
        String direction
) {
}
