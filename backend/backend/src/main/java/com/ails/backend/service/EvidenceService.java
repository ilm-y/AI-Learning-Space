package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.dto.understanding.EvidenceSourceResponse;
import com.ails.backend.entity.EvidenceSource;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceFollowUpStatus;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.mapper.EvidenceSourceMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.DigestUtils;
import org.springframework.util.StringUtils;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 证据链的唯一写入者。
 * <p>
 * <b>写入语义：append-only。</b>已有的 Evidence 永不修改、永不删除 ——
 * 证据之间冲突时不是「改掉旧的」，而是「再追加一条」，让历史保持可追溯。
 * 因此本类没有 update / delete 方法，对外也没有对应的 REST 入口。
 * <p>
 * 关于幂等：目标不是「一个来源只能有一条 Evidence」。同一个 Message 本来就可以为不同
 * Concept 各产生一条 Evidence。真正要防的是<b>同一次 analysis run / 同一个 analyzer
 * 版本重复写入同一条观察</b>，所以按下面两个键查重：
 * <pre>
 *   agent_run_id + concept_id + observation_hash          （agent_run_id 非空时）
 *   source_type + source_id + concept_id + analyzer_version + observation_hash
 * </pre>
 * 数据库侧另加 UNIQUE(agent_run_id, concept_id, observation_hash) 兜底；MySQL 唯一索引
 * 允许多个 NULL，所以没有 run 上下文的手工写入不受该约束限制。
 */
@Service
@RequiredArgsConstructor
public class EvidenceService {

    private final LearningEvidenceMapper evidenceMapper;
    private final EvidenceSourceMapper evidenceSourceMapper;

    /**
     * 一条待写入的证据。
     *
     * @param agentRunId       产生它的 analysis run，可为空
     * @param analyzerVersion  分析器版本，可为空
     * @param observationHash  观察内容归一化后的 hash，为空时自动计算
     * @param sources          真实来源，至少一个（Message / Highlight / TestAnswer）
     */
    public record Draft(
            Long userId,
            Long conceptId,
            Long understandingId,
            String observation,
            EvidenceCorrectness correctness,
            Float reasoningQuality,
            Float specificity,
            Float aiConfidence,
            Boolean needFollowUp,
            Long agentRunId,
            String analyzerVersion,
            String observationHash,
            List<SourceRef> sources) {
    }

    /** Evidence 的多态来源引用 */
    public record SourceRef(EvidenceSourceType sourceType, Long sourceId) {
    }

    /**
     * 追加一条证据。命中查重键时跳过写入并返回 {@code null}，由调用方决定是否继续后续链路。
     * <p>
     * 注意：即使跳过了 Evidence，调用方通常仍会继续跑 Understanding 投影 —— 因为重复的
     * Evidence 本来就不会带来快照变化，投影出来自然也是空操作。
     */
    @Transactional
    public LearningEvidence append(Draft draft) {
        if (draft.sources() == null || draft.sources().isEmpty()) {
            throw new BusinessException(500, "Evidence 必须至少有一个来源");
        }

        String hash = StringUtils.hasText(draft.observationHash())
                ? draft.observationHash()
                : hashObservation(draft.observation());

        if (isDuplicate(draft, hash)) {
            return null;
        }

        LearningEvidence evidence = new LearningEvidence();
        evidence.setUserId(draft.userId());
        evidence.setConceptId(draft.conceptId());
        evidence.setUnderstandingId(draft.understandingId());
        evidence.setObservation(draft.observation());
        evidence.setCorrectness(draft.correctness() == null ? EvidenceCorrectness.UNKNOWN : draft.correctness());
        evidence.setReasoningQuality(draft.reasoningQuality());
        evidence.setSpecificity(draft.specificity());
        evidence.setAiConfidence(draft.aiConfidence());
        evidence.setNeedFollowUp(Boolean.TRUE.equals(draft.needFollowUp()));
        evidence.setFollowUpStatus(Boolean.TRUE.equals(draft.needFollowUp())
                ? EvidenceFollowUpStatus.PENDING : EvidenceFollowUpStatus.NONE);
        evidence.setAgentRunId(draft.agentRunId());
        evidence.setAnalyzerVersion(draft.analyzerVersion());
        evidence.setObservationHash(hash);
        evidenceMapper.insert(evidence);

        for (SourceRef ref : draft.sources()) {
            EvidenceSource source = new EvidenceSource();
            source.setEvidenceId(evidence.getId());
            source.setSourceType(ref.sourceType());
            source.setSourceId(ref.sourceId());
            evidenceSourceMapper.insert(source);
        }
        return evidence;
    }

    // ================= 查询 =================

    @Transactional(readOnly = true)
    public List<EvidenceResponse> listByConcept(Long userId, Long conceptId) {
        return evidenceMapper.selectList(new LambdaQueryWrapper<LearningEvidence>()
                        .eq(LearningEvidence::getUserId, userId)
                        .eq(LearningEvidence::getConceptId, conceptId)
                        .orderByDesc(LearningEvidence::getCreatedAt)
                        .orderByDesc(LearningEvidence::getId))
                .stream()
                .map(e -> EvidenceResponse.from(e, loadSources(e.getId())))
                .toList();
    }

    /**
     * 单条证据详情：前端靠 sources 里的 sourceType + sourceId 跳回原始消息 / 批注 / 测试回答。
     */
    @Transactional(readOnly = true)
    public EvidenceResponse get(Long userId, Long evidenceId) {
        LearningEvidence evidence = evidenceMapper.selectById(evidenceId);
        if (evidence == null || !Objects.equals(evidence.getUserId(), userId)) {
            throw BusinessException.notFound("证据不存在");
        }
        return EvidenceResponse.from(evidence, loadSources(evidenceId));
    }

    @Transactional(readOnly = true)
    public List<EvidenceSourceResponse> loadSources(Long evidenceId) {
        return evidenceSourceMapper.selectList(new LambdaQueryWrapper<EvidenceSource>()
                        .eq(EvidenceSource::getEvidenceId, evidenceId)
                        .orderByAsc(EvidenceSource::getId))
                .stream().map(EvidenceSourceResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public long countByConcept(Long userId, Long conceptId) {
        return evidenceMapper.selectCount(new LambdaQueryWrapper<LearningEvidence>()
                .eq(LearningEvidence::getUserId, userId)
                .eq(LearningEvidence::getConceptId, conceptId));
    }

    // ================= 内部工具 =================

    private boolean isDuplicate(Draft draft, String hash) {
        if (draft.agentRunId() != null) {
            // 同一次 run 内，同一个 Concept 的同一条观察只写一次
            Long same = evidenceMapper.selectCount(new LambdaQueryWrapper<LearningEvidence>()
                    .eq(LearningEvidence::getAgentRunId, draft.agentRunId())
                    .eq(LearningEvidence::getConceptId, draft.conceptId())
                    .eq(LearningEvidence::getObservationHash, hash));
            if (same != null && same > 0) {
                return true;
            }
        }
        if (!StringUtils.hasText(draft.analyzerVersion())) {
            return false;
        }
        // 跨 run 的重复分析：同一个来源、同一个 analyzer 版本、同一个 Concept、同一条观察
        Set<Long> candidateIds = draft.sources().stream()
                .map(ref -> evidenceSourceMapper.selectList(new LambdaQueryWrapper<EvidenceSource>()
                        .eq(EvidenceSource::getSourceType, ref.sourceType())
                        .eq(EvidenceSource::getSourceId, ref.sourceId())))
                .flatMap(List::stream)
                .map(EvidenceSource::getEvidenceId)
                .collect(Collectors.toSet());
        if (candidateIds.isEmpty()) {
            return false;
        }
        Long same = evidenceMapper.selectCount(new LambdaQueryWrapper<LearningEvidence>()
                .in(LearningEvidence::getId, candidateIds)
                .eq(LearningEvidence::getConceptId, draft.conceptId())
                .eq(LearningEvidence::getAnalyzerVersion, draft.analyzerVersion())
                .eq(LearningEvidence::getObservationHash, hash));
        return same != null && same > 0;
    }

    /**
     * 观察内容的归一化 hash：折叠空白、去首尾、转小写，再取 MD5。
     * 只是去重用的指纹，不做安全用途。
     */
    private static String hashObservation(String observation) {
        String normalized = observation == null ? "" : observation.trim().replaceAll("\\s+", " ").toLowerCase();
        return DigestUtils.md5DigestAsHex(normalized.getBytes(StandardCharsets.UTF_8));
    }
}
