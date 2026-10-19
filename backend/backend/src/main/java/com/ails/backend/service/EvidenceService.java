package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.dto.understanding.EvidenceSourceResponse;
import com.ails.backend.entity.EvidenceSource;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceFollowUpStatus;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.mapper.EvidenceSourceMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import com.ails.backend.utils.Hashing;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Objects;

/**
 * 证据链的唯一写入者。
 * <p>
 * <b>写入语义：append-only。</b>已有的 Evidence 永不修改、永不删除 ——
 * 证据之间冲突时不是「改掉旧的」，而是「再追加一条」，让历史保持可追溯。
 * 因此本类没有 update / delete 方法，对外也没有对应的 REST 入口。
 * <p>
 * <b>去重口径：{@code dedupe_key} 是 Evidence 的唯一身份</b>（见 {@code docs/database&API_design.md}
 * §十四~十六、§二十九）。它的值由
 * <pre>
 *   SHA256(user_id + concept_id + evidence_type + 主来源 + normalize(observation))
 * </pre>
 * 确定性算出，数据库上有 {@code UNIQUE(dedupe_key)} 兜底。关键是<b>不含 agent_run_id</b>：
 * 同一个 Message 被两个 run 各分析一次，是两件执行记录、但只是同一条 Evidence，
 * 不该因为「开了两次 run」就多出一条。{@code agent_run_id} 因此降级为溯源元数据。
 * <p>
 * {@link #append} 是 upsert：命中已有 {@code dedupe_key} 时不新增行，只补挂新的
 * {@link EvidenceSource} 并把 {@code last_seen_run_id} 推进到本次 run。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class EvidenceService {

    private final LearningEvidenceMapper evidenceMapper;
    private final EvidenceSourceMapper evidenceSourceMapper;

    /**
     * 一条待写入的证据。
     *
     * @param evidenceType     这条证据是怎么来的，参与 dedupe_key，不可为空
     * @param agentRunId       本次产生的 run。写新行时记进 first_seen_run_id / last_seen_run_id；
     *                         命中已有行时只推进 last_seen_run_id
     * @param analyzerVersion  分析器版本，纯溯源用，<b>不再参与去重</b>
     *                         （观察内容真的变了，dedupe_key 自然就变了，不需要版本号兜着）
     * @param sources          真实来源，至少一个。**第一个是主来源**，参与 dedupe_key；
     *                         其余只做溯源，顺序无意义但必须稳定
     */
    public record Draft(
            Long userId,
            Long conceptId,
            Long understandingId,
            EvidenceType evidenceType,
            String observation,
            EvidenceCorrectness correctness,
            Float reasoningQuality,
            Float specificity,
            Float aiConfidence,
            Boolean needFollowUp,
            Long agentRunId,
            String analyzerVersion,
            List<SourceRef> sources) {
    }

    /** Evidence 的多态来源引用 */
    public record SourceRef(EvidenceSourceType sourceType, Long sourceId) {
    }

    /**
     * {@link #append} 的结果。
     *
     * @param created true = 这次真的新增了一条 Evidence；false = 命中已有的一模一样的证据，
     *                数据库里没有新行（但来源可能被补挂了）
     */
    public record AppendResult(LearningEvidence evidence, boolean created) {
    }

    /**
     * 追加一条证据（upsert 语义）。
     * <p>
     * 命中 {@code dedupe_key} 时返回 {@code created = false}，调用方据此<b>跳过</b>
     * 后续的 Understanding 投影 —— 同一条观察重复进来，快照本来也不会变，
     * 再跑一遍只会白白产生一条内容相同的 Revision。
     */
    @Transactional
    public AppendResult append(Draft draft) {
        if (draft.sources() == null || draft.sources().isEmpty()) {
            throw new BusinessException(500, "Evidence 必须至少有一个来源");
        }
        if (draft.evidenceType() == null) {
            throw new BusinessException(500, "Evidence 必须指明类型");
        }

        String dedupeKey = dedupeKey(draft);
        LearningEvidence existing = findByDedupeKey(dedupeKey);
        if (existing != null) {
            return new AppendResult(seenAgain(existing, draft), false);
        }

        LearningEvidence evidence = new LearningEvidence();
        evidence.setUserId(draft.userId());
        evidence.setConceptId(draft.conceptId());
        evidence.setUnderstandingId(draft.understandingId());
        evidence.setEvidenceType(draft.evidenceType());
        evidence.setObservation(draft.observation());
        evidence.setCorrectness(draft.correctness() == null ? EvidenceCorrectness.UNKNOWN : draft.correctness());
        evidence.setReasoningQuality(draft.reasoningQuality());
        evidence.setSpecificity(draft.specificity());
        evidence.setAiConfidence(draft.aiConfidence());
        evidence.setNeedFollowUp(Boolean.TRUE.equals(draft.needFollowUp()));
        evidence.setFollowUpStatus(Boolean.TRUE.equals(draft.needFollowUp())
                ? EvidenceFollowUpStatus.PENDING : EvidenceFollowUpStatus.NONE);
        evidence.setFirstSeenRunId(draft.agentRunId());
        evidence.setLastSeenRunId(draft.agentRunId());
        evidence.setAnalyzerVersion(draft.analyzerVersion());
        evidence.setDedupeKey(dedupeKey);

        try {
            evidenceMapper.insert(evidence);
        } catch (DuplicateKeyException e) {
            // 唯一约束是最终保证，这里是真的会撞上的：两个并发 run 分析同一个 Message 时，
            // 上面那次 findByDedupeKey 会双双扑空。MySQL 在遇到重复键后不会废掉整个事务，
            // 所以这里可以安静地退化成「再次看到已有证据」这条路。
            log.debug("Evidence dedupe_key 并发命中, key={}, runId={}", dedupeKey, draft.agentRunId());
            LearningEvidence concurrent = findByDedupeKey(dedupeKey);
            if (concurrent == null) {
                throw e; // 不是并发去重，是别的完整性冲突，照常往上抛
            }
            return new AppendResult(seenAgain(concurrent, draft), false);
        }

        attachSources(evidence.getId(), draft.sources());
        return new AppendResult(evidence, true);
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

    /**
     * 「又看到同一条证据」：不新增行，但把这次带来的新来源补挂上去，
     * 并把 last_seen_run_id 推进到本次 run（文档 §二十九的 ON DUPLICATE KEY UPDATE 部分）。
     */
    private LearningEvidence seenAgain(LearningEvidence existing, Draft draft) {
        attachSources(existing.getId(), draft.sources());

        Long runId = draft.agentRunId();
        if (runId != null && !runId.equals(existing.getLastSeenRunId())) {
            LearningEvidence touch = new LearningEvidence();
            touch.setId(existing.getId());
            touch.setLastSeenRunId(runId);
            evidenceMapper.updateById(touch); // updated_at 由 MetaObjectHandler 自动填
            existing.setLastSeenRunId(runId);
        }
        return existing;
    }

    /** 补挂来源。evidence_source 上有 UNIQUE(evidence_id, source_type, source_id)，先查后插避免撞约束。 */
    private void attachSources(Long evidenceId, List<SourceRef> sources) {
        for (SourceRef ref : sources) {
            Long already = evidenceSourceMapper.selectCount(new LambdaQueryWrapper<EvidenceSource>()
                    .eq(EvidenceSource::getEvidenceId, evidenceId)
                    .eq(EvidenceSource::getSourceType, ref.sourceType())
                    .eq(EvidenceSource::getSourceId, ref.sourceId()));
            if (already != null && already > 0) {
                continue;
            }
            EvidenceSource source = new EvidenceSource();
            source.setEvidenceId(evidenceId);
            source.setSourceType(ref.sourceType());
            source.setSourceId(ref.sourceId());
            evidenceSourceMapper.insert(source);
        }
    }

    private LearningEvidence findByDedupeKey(String dedupeKey) {
        return evidenceMapper.selectOne(new LambdaQueryWrapper<LearningEvidence>()
                .eq(LearningEvidence::getDedupeKey, dedupeKey)
                .last("LIMIT 1"));
    }

    /**
     * 确定性去重指纹，见类注释。
     * <p>
     * 各段之间用 {@code |} 分隔：不加分隔符时 {@code user=1,concept=23} 与
     * {@code user=12,concept=3} 会拼出同一个字符串，两条不相干的证据就撞成一条了。
     */
    static String dedupeKey(Draft draft) {
        SourceRef primary = draft.sources().get(0);
        String raw = draft.userId() + "|"
                + draft.conceptId() + "|"
                + draft.evidenceType().name() + "|"
                + primary.sourceType().name() + "|"
                + primary.sourceId() + "|"
                + normalize(draft.observation());
        return sha256Hex(raw);
    }

    /** 观察内容的归一化：折叠空白、去首尾、统一小写。只影响去重，不改写入的原文。 */
    static String normalize(String observation) {
        return Hashing.normalize(observation);
    }

    static String sha256Hex(String text) {
        return Hashing.sha256Hex(text);
    }
}
