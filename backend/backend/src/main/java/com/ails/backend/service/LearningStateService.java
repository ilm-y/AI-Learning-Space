package com.ails.backend.service;

import com.ails.backend.dto.state.AttentionItem;
import com.ails.backend.dto.state.LearningStateResponse;
import com.ails.backend.dto.state.TopicCoverageItem;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningState;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.UnderstandingRevision;
import com.ails.backend.enums.AttentionReason;
import com.ails.backend.enums.MisconceptionStatus;
import com.ails.backend.enums.UnderstandingStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import com.ails.backend.mapper.LearningStateMapper;
import com.ails.backend.mapper.LearningUnderstandingMapper;
import com.ails.backend.mapper.UnderstandingRevisionMapper;
import com.ails.backend.model.MisconceptionLifecycle;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 学习状态的<b>唯一写入者</b>，同时也是查询入口。
 * <p>
 * LearningState 是 {@link LearningUnderstanding} 的 <b>Materialized Projection</b>：
 * 它不产生任何自己的判断，只是把理解快照的 status 复制过来，方便按状态筛选 / 排序 / 推荐。
 * 因此：
 * <ul>
 *   <li>写入路径只有一条 —— {@link #project}，由 Evidence → Understanding 链路在末尾调用；</li>
 *   <li>对外只提供查询接口，不存在「直接改学习状态」的入口；</li>
 *   <li>没有 mastery / confidence / stability 之类的数值聚合，也不存在阈值。
 *       想知道「为什么是这个状态」，去看 Understanding 的快照与 Revision 历史。</li>
 *   <li>误区是 ACTIVE 还是 PENDING 属于 Understanding：投影只把结论落到 review_priority
 *       （9 / 6），attention 的理由回读快照现算，不在本表里冗余第二份。</li>
 * </ul>
 * 这样就不会出现「Understanding 说 PARTIAL，LearningState 说 DEMONSTRATED」的分裂。
 */
@Service
@RequiredArgsConstructor
public class LearningStateService {

    /** 超过这么久没有新证据，就认为理解需要重新确认 */
    private static final int LONG_UNVERIFIED_DAYS = 14;

    private final LearningStateMapper stateMapper;
    private final LearningEvidenceMapper evidenceMapper;
    private final LearningUnderstandingMapper understandingMapper;
    private final UnderstandingRevisionMapper revisionMapper;
    private final ConceptMapper conceptMapper;
    private final ConceptService conceptService;

    /**
     * 把理解快照投影为学习状态。这是本服务唯一的写入方法。
     */
    @Transactional
    public LearningState project(Long userId, Long conceptId, LearningUnderstanding understanding, LocalDateTime evidenceAt) {
        LearningState state = find(userId, conceptId);
        if (state == null) {
            state = new LearningState();
            state.setUserId(userId);
            state.setConceptId(conceptId);
            state.setReviewPriority(0);
            applyProjection(state, understanding, evidenceAt);
            stateMapper.insert(state);
            return state;
        }
        applyProjection(state, understanding, evidenceAt);
        stateMapper.updateById(state);
        return state;
    }

    // ================= 查询 =================

    @Transactional(readOnly = true)
    public List<LearningStateResponse> overview(Long userId) {
        List<LearningState> states = stateMapper.selectList(new LambdaQueryWrapper<LearningState>()
                .eq(LearningState::getUserId, userId)
                .orderByDesc(LearningState::getReviewPriority)
                .orderByDesc(LearningState::getUpdatedAt));
        Map<Long, Concept> concepts = loadConcepts(states.stream().map(LearningState::getConceptId).toList());
        return states.stream()
                .map(state -> LearningStateResponse.from(state, conceptName(concepts.get(state.getConceptId()))))
                .toList();
    }

    @Transactional(readOnly = true)
    public LearningStateResponse conceptDetail(Long userId, Long conceptId) {
        conceptService.accessibleConcept(userId, conceptId);
        LearningState state = find(userId, conceptId);
        if (state == null) {
            // 还没有任何证据时没有投影行，按 UNKNOWN 返回，而不是编一个分数
            return new LearningStateResponse(userId, conceptId, conceptNameOf(conceptId),
                    UnderstandingStatus.UNKNOWN, null, null, 0, null);
        }
        return LearningStateResponse.from(state, conceptNameOf(conceptId));
    }

    /**
     * 「现在最该关注什么」。
     * <p>
     * 取代了早期的 Dashboard 数值看板：不做分数聚合，直接回答「下一步该碰哪个概念、为什么」。
     */
    @Transactional(readOnly = true)
    public List<AttentionItem> attention(Long userId, int limit) {
        List<LearningState> states = stateMapper.selectList(new LambdaQueryWrapper<LearningState>()
                .eq(LearningState::getUserId, userId)
                .orderByDesc(LearningState::getReviewPriority)
                .orderByAsc(LearningState::getNextReviewAt));
        Map<Long, Concept> concepts = loadConcepts(states.stream().map(LearningState::getConceptId).toList());
        // ACTIVE / PENDING 的区分属于 Understanding，不在 learning_state 里冗余一份
        Map<Long, MisconceptionStatus> misconceptions = loadMisconceptions(userId,
                states.stream().map(LearningState::getConceptId).toList());
        LocalDateTime now = LocalDateTime.now();

        return states.stream()
                .map(state -> new AttentionItem(
                        state.getConceptId(),
                        conceptName(concepts.get(state.getConceptId())),
                        state.getStatus(),
                        reasonOf(state, misconceptions.get(state.getConceptId()), now),
                        state.getLastEvidenceAt(),
                        state.getNextReviewAt(),
                        state.getReviewPriority()))
                .filter(item -> item.reason() != null)
                .sorted(Comparator.comparingInt((AttentionItem item) -> item.reviewPriority() == null ? 0 : item.reviewPriority()).reversed())
                .limit(Math.max(1, limit))
                .toList();
    }

    /**
     * 某个学习空间下的知识点覆盖情况：哪些只是碰过、哪些真的建立了理解。
     */
    @Transactional(readOnly = true)
    public List<TopicCoverageItem> topicCoverage(Long userId, Long spaceId) {
        List<Concept> concepts = conceptMapper.selectList(new LambdaQueryWrapper<Concept>()
                .eq(Concept::getLearningSpaceId, spaceId)
                .orderByAsc(Concept::getId));
        if (concepts.isEmpty()) {
            return List.of();
        }
        List<Long> conceptIds = concepts.stream().map(Concept::getId).toList();

        Map<Long, LearningState> states = stateMapper.selectList(new LambdaQueryWrapper<LearningState>()
                        .eq(LearningState::getUserId, userId)
                        .in(LearningState::getConceptId, conceptIds))
                .stream().collect(Collectors.toMap(LearningState::getConceptId, Function.identity(), (a, b) -> a));

        Map<Long, Long> evidenceCounts = groupCount(evidenceMapper.selectList(
                new LambdaQueryWrapper<LearningEvidence>()
                        .eq(LearningEvidence::getUserId, userId)
                        .in(LearningEvidence::getConceptId, conceptIds))
                .stream().map(LearningEvidence::getConceptId).toList());

        Map<Long, Long> conceptByUnderstanding = understandingMapper.selectList(
                        new LambdaQueryWrapper<LearningUnderstanding>()
                                .eq(LearningUnderstanding::getUserId, userId)
                                .in(LearningUnderstanding::getConceptId, conceptIds))
                .stream()
                .collect(Collectors.toMap(LearningUnderstanding::getId, LearningUnderstanding::getConceptId));
        Map<Long, Long> revisionCounts = conceptByUnderstanding.isEmpty() ? Map.of()
                : groupCount(revisionMapper.selectList(new LambdaQueryWrapper<UnderstandingRevision>()
                                .in(UnderstandingRevision::getUnderstandingId, conceptByUnderstanding.keySet()))
                        .stream().map(r -> conceptByUnderstanding.get(r.getUnderstandingId()))
                        .toList());

        return concepts.stream().map(concept -> {
            LearningState state = states.get(concept.getId());
            UnderstandingStatus status = state == null ? UnderstandingStatus.UNKNOWN : state.getStatus();
            return new TopicCoverageItem(
                    concept.getId(),
                    conceptName(concept),
                    status,
                    evidenceCounts.getOrDefault(concept.getId(), 0L).intValue(),
                    revisionCounts.getOrDefault(concept.getId(), 0L).intValue(),
                    state == null ? null : state.getLastEvidenceAt());
        }).toList();
    }

    // ================= 内部工具 =================

    private void applyProjection(LearningState state, LearningUnderstanding understanding, LocalDateTime evidenceAt) {
        UnderstandingStatus status = understanding == null || understanding.getStatus() == null
                ? UnderstandingStatus.UNKNOWN : understanding.getStatus();
        MisconceptionStatus misconception = misconceptionOf(understanding);
        state.setStatus(status);
        if (evidenceAt != null) {
            state.setLastEvidenceAt(evidenceAt);
        }
        LocalDateTime base = state.getLastEvidenceAt() == null ? LocalDateTime.now() : state.getLastEvidenceAt();
        state.setNextReviewAt(base.plusDays(reviewIntervalDays(status, misconception)));
        state.setReviewPriority(reviewPriority(status, misconception));
    }

    /** 复习间隔与优先级都由状态派生，不引入任何数值分数 */
    private int reviewIntervalDays(UnderstandingStatus status, MisconceptionStatus misconception) {
        return switch (status) {
            // 已经出现反向证据的误区不用天天盯，隔天再看有没有稳定下来
            case MISCONCEPTION -> misconception == MisconceptionStatus.PENDING ? 2 : 1;
            case UNKNOWN -> 1;
            case EXPOSED -> 2;
            case PARTIAL -> 3;
            case DEMONSTRATED -> 7;
        };
    }

    /**
     * 优先级阶梯：仍成立的误区最高，正在解除的误区次之，其余按理解程度递减。
     * <p>
     * 「曾经有误区」不占权重 —— 误区一旦解除就不再出现在快照里，也就不会继续压着
     * 这个概念排在 attention 最前面。
     */
    private int reviewPriority(UnderstandingStatus status, MisconceptionStatus misconception) {
        return switch (status) {
            case MISCONCEPTION -> misconception == MisconceptionStatus.PENDING ? 6 : 9;
            case PARTIAL -> 5;
            case EXPOSED -> 4;
            case UNKNOWN -> 3;
            case DEMONSTRATED -> 1;
        };
    }

    private AttentionReason reasonOf(LearningState state, MisconceptionStatus misconception, LocalDateTime now) {
        // 兜底：万一读不到快照，退回状态判定，别把 MISCONCEPTION 漏出 attention
        if (misconception == MisconceptionStatus.ACTIVE
                || (misconception == null && state.getStatus() == UnderstandingStatus.MISCONCEPTION)) {
            return AttentionReason.MISCONCEPTION;
        }
        if (misconception == MisconceptionStatus.PENDING) {
            return AttentionReason.MISCONCEPTION_RESOLVING;
        }
        if (state.getLastEvidenceAt() != null
                && state.getLastEvidenceAt().isBefore(now.minusDays(LONG_UNVERIFIED_DAYS))
                && state.getStatus() != UnderstandingStatus.DEMONSTRATED) {
            return AttentionReason.LONG_UNVERIFIED;
        }
        if (state.getNextReviewAt() != null && !state.getNextReviewAt().isAfter(now)) {
            return AttentionReason.NEEDS_REVIEW;
        }
        return null;
    }

    private LearningState find(Long userId, Long conceptId) {
        return stateMapper.selectOne(new LambdaQueryWrapper<LearningState>()
                .eq(LearningState::getUserId, userId)
                .eq(LearningState::getConceptId, conceptId));
    }

    private Map<Long, Long> groupCount(List<Long> ids) {
        return ids.stream()
                .filter(Objects::nonNull)
                .collect(Collectors.groupingBy(Function.identity(), Collectors.counting()));
    }

    /** 每个 Concept 当前误区的严重程度（ACTIVE > PENDING > 无），一次查询取回 */
    private Map<Long, MisconceptionStatus> loadMisconceptions(Long userId, List<Long> conceptIds) {
        List<Long> distinct = conceptIds.stream().filter(Objects::nonNull).distinct().toList();
        if (distinct.isEmpty()) {
            return Collections.emptyMap();
        }
        Map<Long, MisconceptionStatus> result = new HashMap<>();
        for (LearningUnderstanding understanding : understandingMapper.selectList(
                new LambdaQueryWrapper<LearningUnderstanding>()
                        .eq(LearningUnderstanding::getUserId, userId)
                        .in(LearningUnderstanding::getConceptId, distinct))) {
            MisconceptionStatus severity =
                    MisconceptionLifecycle.severity(MisconceptionLifecycle.parse(understanding.getMisconceptions()));
            if (severity != null) {
                result.put(understanding.getConceptId(), severity);
            }
        }
        return result;
    }

    /** 快照里当前的误区严重程度；没有未解除的误区时返回 null */
    private MisconceptionStatus misconceptionOf(LearningUnderstanding understanding) {
        if (understanding == null) {
            return null;
        }
        return MisconceptionLifecycle.severity(MisconceptionLifecycle.parse(understanding.getMisconceptions()));
    }

    private Map<Long, Concept> loadConcepts(List<Long> ids) {
        List<Long> distinct = ids.stream().filter(Objects::nonNull).distinct().toList();
        if (distinct.isEmpty()) {
            return Collections.emptyMap();
        }
        return conceptMapper.selectBatchIds(distinct).stream()
                .collect(Collectors.toMap(Concept::getId, Function.identity()));
    }

    private String conceptNameOf(Long conceptId) {
        Concept concept = conceptMapper.selectById(conceptId);
        return conceptName(concept);
    }

    private String conceptName(Concept concept) {
        if (concept == null) {
            return null;
        }
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }
}
