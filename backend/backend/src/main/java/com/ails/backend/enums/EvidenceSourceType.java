package com.ails.backend.enums;

/**
 * Evidence 的真实来源类型（evidence_source.source_type）。
 * <p>
 * source_id 是多态引用，由应用层校验真实对象存在。
 */
public enum EvidenceSourceType {
    MESSAGE,
    HIGHLIGHT,
    TEST_ANSWER
}
