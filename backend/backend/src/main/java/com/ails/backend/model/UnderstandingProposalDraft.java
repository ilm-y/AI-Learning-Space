package com.ails.backend.model;

import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.List;

/**
 * Understanding 的增量提案（与 Web DTO 解耦）。
 * <p>
 * Agent 只说「本轮新增了什么」，合并规则由
 * {@link com.ails.backend.service.UnderstandingService} 负责。把 DTO 直接传进
 * service 会让 Web 层的字段改名波及持久化逻辑，所以这里保留一个只含业务字段的模型。
 */
public record UnderstandingProposalDraft(
        List<String> knownParts,
        List<String> uncertainParts,
        List<String> misconceptions,
        String latestUserExplanation) {

    public UnderstandingProposalDraft {
        knownParts = clean(knownParts);
        uncertainParts = clean(uncertainParts);
        misconceptions = clean(misconceptions);
        latestUserExplanation = StringUtils.hasText(latestUserExplanation)
                ? latestUserExplanation.trim() : null;
    }

    /** 全部为空时不需要读快照，也不需要产生 Revision。 */
    public boolean isEmpty() {
        return knownParts.isEmpty()
                && uncertainParts.isEmpty()
                && misconceptions.isEmpty()
                && latestUserExplanation == null;
    }

    private static List<String> clean(List<String> values) {
        if (values == null || values.isEmpty()) {
            return List.of();
        }
        List<String> result = new ArrayList<>();
        for (String value : values) {
            if (!StringUtils.hasText(value)) {
                continue;
            }
            String trimmed = value.trim().replaceAll("\\s+", " ");
            if (!result.contains(trimmed)) {
                result.add(trimmed);
            }
        }
        return List.copyOf(result);
    }
}
