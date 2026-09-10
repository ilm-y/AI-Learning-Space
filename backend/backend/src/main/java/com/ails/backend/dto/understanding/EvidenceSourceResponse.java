package com.ails.backend.dto.understanding;

import com.ails.backend.entity.EvidenceSource;
import com.ails.backend.enums.EvidenceSourceType;

/**
 * Evidence 的原始来源。
 * <p>
 * 前端拿到 sourceType + sourceId 后可以跳回原始消息 / 批注 / 测试回答。
 */
public record EvidenceSourceResponse(EvidenceSourceType sourceType, Long sourceId) {

    public static EvidenceSourceResponse from(EvidenceSource source) {
        if (source == null) {
            return null;
        }
        return new EvidenceSourceResponse(source.getSourceType(), source.getSourceId());
    }
}
