package com.ails.backend.model;

import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.MisconceptionStatus;
import com.ails.backend.utils.JsonUtil;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * 误区的状态迁移规则 —— 纯函数，不碰数据库，可以单独测。
 * <p>
 * <pre>
 *   错误证据            → ACTIVE    当前存在这个误区
 *   ACTIVE  + 反向正确  → PENDING   可能正在解除
 *   PENDING + 再次确认  → 移除      视为已解除
 *   移除后又有错误证据  → ACTIVE    重新出现，按新一轮重新计时
 * </pre>
 * <b>历史永远不会被抹掉</b>：这里只改「当前快照」，每一步变化都会由
 * UnderstandingService 落成一条 understanding_revision，证据本身在
 * learning_evidence 里 append-only 地留着。
 * <p>
 * 「几次独立确认算解除」是这里的常量，不是数据库字段 —— 将来要升级成
 * 「一次高质量解释 + 一次测试正确」这种更细的规则，只改这个类，
 * 数据模型和已经写入的历史都不用动。
 */
public final class MisconceptionLifecycle {

    /**
     * 认定误区已经解除所需的<b>独立</b>反向证据条数。
     * <p>
     * 一次答对只说明「这一次答对了」，所以第一次反向证据先把误区降到 PENDING，
     * 第二次才解除。Evidence 是按 source 去重的，两条不同的证据必然是两次
     * 不同的 Message / Highlight / TestAnswer，天然「独立」。
     */
    public static final int REQUIRED_CONFIRMATIONS = 2;

    /** 低于这个推理质量的反向证据不予采信（quality 未知时不设限） */
    public static final float MIN_REASONING_QUALITY = 0.5f;

    private MisconceptionLifecycle() {
    }

    /**
     * 用一条新证据推进误区列表。
     * <p>
     * 规则是「够不够强」而不是「谁对谁错」：
     * <ul>
     *   <li>{@code INCORRECT} → 激活/刷新对应误区</li>
     *   <li>{@code CORRECT} 且推理质量达标 → 计一次反向确认</li>
     *   <li>{@code PARTIAL} / {@code UNKNOWN} → 不参与误区判定</li>
     * </ul>
     * 返回的内容与入参相同时，说明这次证据没有改变任何判断，
     * 上层据此判断是否要写 Revision。
     * <p>
     * 已知的简化：本地规则版分析器分不清一条正确证据到底反驳了<b>哪一个</b>误区，
     * 因此它对该 Concept 下所有未解除的误区一视同仁地计一次确认。接入 Agent 后
     * 可以由 Agent 指明针对的是哪一条。
     */
    public static List<Misconception> evolve(List<Misconception> current, LearningEvidence evidence) {
        if (evidence == null || evidence.getCorrectness() == null) {
            return current;
        }
        List<Misconception> result = new ArrayList<>(current == null ? List.of() : current);
        LocalDateTime at = evidence.getCreatedAt() == null ? LocalDateTime.now() : evidence.getCreatedAt();

        return switch (evidence.getCorrectness()) {
            case INCORRECT -> activate(result, evidence, at);
            case CORRECT -> reverse(result, evidence, at);
            case PARTIAL, UNKNOWN -> result;
        };
    }

    /** 错误证据：已有同名误区就重新激活，否则新增一条 ACTIVE */
    private static List<Misconception> activate(List<Misconception> list, LearningEvidence evidence, LocalDateTime at) {
        String text = normalize(evidence.getObservation());
        if (!StringUtils.hasText(text)) {
            return list;
        }
        for (int i = 0; i < list.size(); i++) {
            Misconception existing = list.get(i);
            if (!sameText(existing.text(), text)) {
                continue;
            }
            if (existing.status() == MisconceptionStatus.ACTIVE
                    && Objects.equals(existing.lastEvidenceAt(), at)) {
                // 极端情况下的重复投递：内容完全没变
                return list;
            }
            list.set(i, new Misconception(text,
                    MisconceptionStatus.ACTIVE,
                    existing.firstDetectedAt() == null ? at : existing.firstDetectedAt(),
                    at,
                    List.of()));
            return list;
        }
        list.add(new Misconception(text, MisconceptionStatus.ACTIVE, at, at, List.of()));
        return list;
    }

    /** 正确证据：累加一次独立确认，够数就移除（解除），否则降到 PENDING */
    private static List<Misconception> reverse(List<Misconception> list, LearningEvidence evidence, LocalDateTime at) {
        if (list.isEmpty() || !strongEnough(evidence)) {
            return list;
        }
        Long evidenceId = evidence.getId();
        if (evidenceId == null) {
            // apply 之前证据必定已落库；拿不到 id 就没法保证幂等，宁可不改
            return list;
        }
        List<Misconception> survivors = new ArrayList<>(list.size());
        for (Misconception entry : list) {
            if (entry.reversalEvidenceIds().contains(evidenceId)) {
                // 同一条证据重复投递：不重复计数
                survivors.add(entry);
                continue;
            }
            List<Long> confirmed = new ArrayList<>(entry.reversalEvidenceIds());
            confirmed.add(evidenceId);
            if (confirmed.size() >= REQUIRED_CONFIRMATIONS) {
                // 解除：从当前快照移除，历史留在 Evidence 与 Revision 里
                continue;
            }
            survivors.add(new Misconception(entry.text(), MisconceptionStatus.PENDING,
                    entry.firstDetectedAt(), at, confirmed));
        }
        return survivors;
    }

    private static boolean strongEnough(LearningEvidence evidence) {
        Float quality = evidence.getReasoningQuality();
        return quality == null || quality >= MIN_REASONING_QUALITY;
    }

    // ================= 读写 =================

    public static List<Misconception> parse(String json) {
        return JsonUtil.toList(json, Misconception.class);
    }

    public static String serialize(List<Misconception> list) {
        return JsonUtil.toJson(list == null ? List.of() : list);
    }

    /** 当前快照里还没解除的误区（ACTIVE 或 PENDING） */
    public static boolean hasUnresolved(List<Misconception> list) {
        return list != null && !list.isEmpty();
    }

    /** 按严重程度汇总：有 ACTIVE 就是 ACTIVE，否则 PENDING，都没有则 null */
    public static MisconceptionStatus severity(List<Misconception> list) {
        if (list == null || list.isEmpty()) {
            return null;
        }
        for (Misconception entry : list) {
            if (entry.status() == MisconceptionStatus.ACTIVE || entry.status() == null) {
                return MisconceptionStatus.ACTIVE;
            }
        }
        return MisconceptionStatus.PENDING;
    }

    public static List<String> textsOf(List<Misconception> list, MisconceptionStatus status) {
        if (list == null) {
            return List.of();
        }
        return list.stream()
                .filter(entry -> entry.status() == status)
                .map(Misconception::text)
                .filter(StringUtils::hasText)
                .toList();
    }

    private static boolean sameText(String a, String b) {
        return a != null && a.equals(b);
    }

    private static String normalize(String text) {
        return text == null ? null : text.trim().replaceAll("\\s+", " ");
    }
}
