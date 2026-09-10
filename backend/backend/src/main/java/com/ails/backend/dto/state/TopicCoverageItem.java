package com.ails.backend.dto.state;

import com.ails.backend.enums.UnderstandingStatus;

import java.time.LocalDateTime;

/**
 * 某个学习空间下单个知识点的覆盖情况。
 * <p>
 * 用来回答「这个空间里我到底学了多少、哪些只是碰过、哪些真的建立了理解」。
 */
public record TopicCoverageItem(
        Long conceptId,
        String conceptName,
        UnderstandingStatus status,
        Integer evidenceCount,
        Integer revisionCount,
        LocalDateTime lastEvidenceAt) {
}
