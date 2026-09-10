package com.ails.backend.model;

import com.ails.backend.enums.MisconceptionStatus;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 当前快照里的一个误区条目，序列化后存在 learning_understanding.misconceptions。
 * <p>
 * 关键语义：<b>它表示「现在还成立」的误区，不是「历史上出现过的错误」。</b>
 * 所以这里没有 RESOLVED 状态 —— 解除即从列表里移除。
 * <p>
 * <pre>
 * {
 *   "text": "把 TCP 三次握手理解成保证数据传输可靠",
 *   "status": "ACTIVE",
 *   "firstDetectedAt": "2026-09-10T10:00:00",
 *   "lastEvidenceAt": "2026-09-10T10:20:00",
 *   "reversalEvidenceIds": [7]
 * }
 * </pre>
 *
 * @param text                误区内容（来自证据的 observation）
 * @param status              ACTIVE / PENDING
 * @param firstDetectedAt     <b>本轮</b>误区第一次被发现的时刻。误区解除后重新出现会重新计时 ——
 *                            当前快照按定义不记得已经解除的条目，跨轮次的完整历史在
 *                            learning_evidence 与 understanding_revision 里
 * @param lastEvidenceAt      最近一次触及该误区的证据时刻
 * @param reversalEvidenceIds 已经采信的、独立的反向正确证据 id。
 *                            存 id 而不是计数 —— 「几次算解除」是策略，由
 *                            {@link MisconceptionLifecycle} 的常量决定，不该写进数据模型。
 */
public record Misconception(
        String text,
        MisconceptionStatus status,
        LocalDateTime firstDetectedAt,
        LocalDateTime lastEvidenceAt,
        List<Long> reversalEvidenceIds) {

    public Misconception {
        reversalEvidenceIds = reversalEvidenceIds == null ? List.of() : List.copyOf(reversalEvidenceIds);
    }
}
