package com.ails.backend.service;

import com.ails.backend.dto.state.ContinueLearningItem;
import com.ails.backend.dto.state.LearningEvidenceResponse;
import com.ails.backend.dto.state.LearningStateResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningState;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.LearningStateStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import com.ails.backend.mapper.LearningStateMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 学习状态查询与本地聚合。
 * <p>
 * 证据链 → 学习状态的本地位简单聚合（加权平均 + 状态机），
 * TODO(M4): 正式版应由 Python Learning-State Agent 替换判分与聚合策略。
 */
@Service
@RequiredArgsConstructor
public class LearningStateService {

    /** confidence 低于该值且 mastery 不足时会进入 VERIFYING / 需要出题验证 */
    private static final double VERIFY_THRESHOLD = 0.65;
    /** 低于该 mastery 视为未掌握，可触发验证 */
    private static final double MASTERY_THRESHOLD = 0.8;

    private final LearningStateMapper stateMapper;
    private final LearningEvidenceMapper evidenceMapper;
    private final ConceptMapper conceptMapper;
    private final ConceptService conceptService;

    @Transactional(readOnly = true)
    public List<LearningStateResponse> overview(Long userId) {
        List<LearningState> states = stateMapper.selectList(new LambdaQueryWrapper<LearningState>()
                .eq(LearningState::getUserId, userId)
                .orderByDesc(LearningState::getUpdatedAt));
        Map<Long, Concept> concepts = loadConcepts(
                states.stream().map(LearningState::getConceptId).toList());
        return states.stream()
                .map(state -> toResponse(state, concepts))
                .toList();
    }

    @Transactional(readOnly = true)
    public LearningStateResponse conceptDetail(Long userId, Long conceptId) {
        conceptService.accessibleConcept(userId, conceptId);
        LearningState state = findState(userId, conceptId).orElse(null);
        return toResponse(state, loadConcepts(List.of(conceptId)));
    }

    @Transactional(readOnly = true)
    public List<LearningEvidenceResponse> evidences(Long userId, Long conceptId) {
        conceptService.accessibleConcept(userId, conceptId);
        return evidenceMapper.selectList(new LambdaQueryWrapper<LearningEvidence>()
                        .eq(LearningEvidence::getUserId, userId)
                        .eq(LearningEvidence::getConceptId, conceptId)
                        .orderByDesc(LearningEvidence::getCreatedAt)
                        .orderByDesc(LearningEvidence::getId))
                .stream().map(LearningEvidenceResponse::from).toList();
    }

    /**
     * “继续学习”推荐：待复习知识点（未 MASTERED），按优先级 / 下次复习时间排序。
     */
    @Transactional(readOnly = true)
    public List<ContinueLearningItem> continueLearning(Long userId, int limit) {
        List<LearningState> states = stateMapper.selectList(new LambdaQueryWrapper<LearningState>()
                .eq(LearningState::getUserId, userId)
                .ne(LearningState::getStatus, LearningStateStatus.MASTERED)
                .orderByDesc(LearningState::getReviewPriority)
                .orderByAsc(LearningState::getNextReviewAt)
                .last("LIMIT " + Math.max(1, limit)));
        Map<Long, Concept> concepts = loadConcepts(
                states.stream().map(LearningState::getConceptId).toList());
        return states.stream().map(state -> {
            Concept concept = concepts.get(state.getConceptId());
            return new ContinueLearningItem(
                    state.getConceptId(),
                    conceptName(concept),
                    state.getMastery(),
                    state.getConfidence(),
                    state.getStatus(),
                    state.getNextReviewAt(),
                    state.getReviewPriority());
        }).toList();
    }

    /**
     * 根据某知识点当前全部证据，加权聚合后更新学习状态（插入证据后调用）。
     * 返回更新后的状态实体。
     */
    @Transactional
    public LearningState recompute(Long userId, Long conceptId) {
        List<LearningEvidence> evidences = evidenceMapper.selectList(
                new LambdaQueryWrapper<LearningEvidence>()
                        .eq(LearningEvidence::getUserId, userId)
                        .eq(LearningEvidence::getConceptId, conceptId)
                        .orderByAsc(LearningEvidence::getCreatedAt)
                        .orderByAsc(LearningEvidence::getId));

        LearningState state = findState(userId, conceptId).orElseGet(() -> {
            LearningState created = new LearningState();
            created.setUserId(userId);
            created.setConceptId(conceptId);
            created.setMastery(0d);
            created.setConfidence(0d);
            created.setStability(0.5d);
            created.setStatus(LearningStateStatus.NEW);
            created.setReviewPriority(0);
            stateMapper.insert(created);
            return created;
        });

        if (evidences.isEmpty()) {
            state.setStatus(LearningStateStatus.NEW);
            stateMapper.updateById(state);
            return state;
        }

        // —— 加权平均聚合（简单启发式；正式版由 Python Agent 完成）——
        LearningEvidence last = evidences.get(evidences.size() - 1);
        double scoreSum = 0;
        double strengthSum = 0;
        int correct = 0;
        int partial = 0;
        boolean hasIncorrect = false;
        for (LearningEvidence ev : evidences) {
            double strength = safe(ev.getWeight()) * safe(ev.getAiConfidence());
            strengthSum += strength;
            scoreSum += strength * correctnessScore(ev.getCorrectness());
            if (ev.getCorrectness() == EvidenceCorrectness.CORRECT) {
                correct++;
            } else if (ev.getCorrectness() == EvidenceCorrectness.PARTIALLY_CORRECT) {
                partial++;
            }
            if (ev.getCorrectness() == EvidenceCorrectness.INCORRECT) {
                hasIncorrect = true;
            }
        }
        double avgScore = strengthSum == 0 ? 0 : clamp(scoreSum / strengthSum);
        double stability = 0.3 + 0.7 * (correct + 0.5 * partial) / evidences.size();
        // 证据越充分/越一致，系统置信度越高
        double confidence = 0.5 + (avgScore - 0.5) * 0.6 + Math.min(strengthSum, 5d) * 0.05;

        state.setMastery(round2(avgScore));
        state.setConfidence(round2(clamp(confidence)));
        state.setStability(round2(clamp(stability)));
        state.setLastEvidenceAt(last.getCreatedAt());
        state.setStatus(resolveStatus(last, state.getMastery(), state.getConfidence()));
        LocalDateTime now = LocalDateTime.now();
        state.setNextReviewAt(hasIncorrect ? now.plusDays(1) : now.plusDays(3));
        // 优先级越高越该复习
        int priority = (int) Math.round((1 - state.getMastery()) * 10) + (hasIncorrect ? 3 : 0);
        state.setReviewPriority(Math.min(10, Math.max(0, priority)));
        stateMapper.updateById(state);
        return state;
    }

    // ================= 内部工具 =================

    private LearningStateResponse toResponse(LearningState state, Map<Long, Concept> concepts) {
        LearningState s = state != null ? state : zeroState();
        Concept concept = concepts.get(s.getConceptId());
        return new LearningStateResponse(
                s.getUserId(), s.getConceptId(), conceptName(concept),
                s.getMastery(), s.getConfidence(), s.getStability(), s.getStatus(),
                s.getLastEvidenceAt(), s.getNextReviewAt(), s.getReviewPriority(), s.getUpdatedAt());
    }

    private LearningState zeroState() {
        LearningState s = new LearningState();
        s.setMastery(0d);
        s.setConfidence(0d);
        s.setStability(0.5d);
        s.setStatus(LearningStateStatus.NEW);
        s.setReviewPriority(0);
        return s;
    }

    private java.util.Optional<LearningState> findState(Long userId, Long conceptId) {
        LearningState state = stateMapper.selectOne(new LambdaQueryWrapper<LearningState>()
                .eq(LearningState::getUserId, userId)
                .eq(LearningState::getConceptId, conceptId));
        return java.util.Optional.ofNullable(state);
    }

    private Map<Long, Concept> loadConcepts(List<Long> ids) {
        List<Long> distinct = ids.stream().filter(Objects::nonNull).distinct().toList();
        if (distinct.isEmpty()) {
            return Collections.emptyMap();
        }
        return conceptMapper.selectBatchIds(distinct).stream()
                .collect(Collectors.toMap(Concept::getId, Function.identity()));
    }

    private String conceptName(Concept concept) {
        if (concept == null) {
            return null;
        }
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }

    private LearningStateStatus resolveStatus(LearningEvidence last, double mastery, double confidence) {
        if (last.getCorrectness() == EvidenceCorrectness.INCORRECT) {
            return LearningStateStatus.MISCONCEPTION;
        }
        if (mastery >= 0.9 && confidence >= 0.85) {
            return LearningStateStatus.MASTERED;
        }
        if (mastery >= 0.7) {
            return LearningStateStatus.PRACTICING;
        }
        if (mastery >= 0.4) {
            return LearningStateStatus.UNDERSTANDING;
        }
        return LearningStateStatus.EXPLORING;
    }

    private double correctnessScore(EvidenceCorrectness correctness) {
        return switch (correctness) {
            case CORRECT -> 1.0;
            case PARTIALLY_CORRECT -> 0.6;
            case INCORRECT -> 0.0;
            case UNKNOWN -> 0.5;
        };
    }

    private static double safe(Double value) {
        return value == null ? 0 : value;
    }

    private static double clamp(double value) {
        return Math.max(0, Math.min(1, value));
    }

    private static double round2(double value) {
        return Math.round(value * 100) / 100.0;
    }

    /** 供 Verification 策略参考的阈值 */
    public static double verifyThreshold() {
        return VERIFY_THRESHOLD;
    }

    public static double masteryThreshold() {
        return MASTERY_THRESHOLD;
    }
}
