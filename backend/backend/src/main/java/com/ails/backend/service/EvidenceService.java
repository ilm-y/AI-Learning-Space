package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.dto.understanding.EvidenceSourceResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.EvidenceSource;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceFollowUpStatus;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.EvidenceSourceMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import com.ails.backend.utils.Hashing;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;
import java.util.stream.Collectors;

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

    /**
     * 批量 Evidence 查询的服务端边界。
     * <p>
     * 默认值与 Agent 的 {@code evidence_top_k}（默认 3）对齐；即使调用方传入更大的值，
     * 服务端也最多返回 {@link #MAX_RECENT_LIMIT} 条，避免一次请求把整个证据链拖出来。
     */
    public static final int DEFAULT_RECENT_LIMIT = 3;
    public static final int MAX_RECENT_LIMIT = 50;
    /** 一次批量查询允许的 conceptId 上限，防止超长 IN 与跨空间探测。 */
    public static final int MAX_CONCEPTS = 50;

    private final LearningEvidenceMapper evidenceMapper;
    private final EvidenceSourceMapper evidenceSourceMapper;
    private final ConceptMapper conceptMapper;
    private final LearningSpaceService spaceService;

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
     * 跨概念的有界证据查询，供 Agent 的 Evidence 检索使用。
     * <p>
     * 与旧 {@link #listByConcept} 的区别：
     * <ul>
     *   <li>SQL 内 {@code LIMIT}，不在应用层全量拉取后切片；</li>
     *   <li>Sources 用一次 {@code WHERE evidence_id IN (...)} 批量加载，避免 N+1；</li>
     *   <li>显式要求 {@code spaceId}：先校验空间属于当前用户，再要求每个
     *       {@code conceptId} 都属于该空间（含未软删）。</li>
     * </ul>
     * 旧单概念接口保持原样，不改变现有前端与历史调用方的行为。
     *
     * @param userId     当前登录用户
     * @param spaceId    学习空间；必须属于 userId
     * @param conceptIds 要查询的概念；非空、去重、全部属于 spaceId
     * @param limit      请求条数；服务端裁剪到 [1, MAX_RECENT_LIMIT]
     */
    @Transactional(readOnly = true)
    public List<EvidenceResponse> listRecentByConcepts(
            Long userId, Long spaceId, List<Long> conceptIds, Integer limit) {
        if (spaceId == null) {
            throw new BusinessException(400, "必须指定 spaceId");
        }
        // 1) 跨用户 / 已软删空间直接 404，不泄露空间是否存在。
        spaceService.ownedSpace(userId, spaceId);

        // 2) 概念必须属于该空间。selectList 已带 @TableLogic，软删概念会查不到。
        List<Long> ids = normalizeConceptIds(conceptIds);
        Map<Long, Concept> conceptsById = conceptMapper.selectList(
                        new LambdaQueryWrapper<Concept>().in(Concept::getId, ids))
                .stream()
                .filter(concept -> Objects.equals(concept.getLearningSpaceId(), spaceId))
                .collect(Collectors.toMap(
                        Concept::getId, Function.identity(), (left, right) -> left));
        for (Long conceptId : ids) {
            if (!conceptsById.containsKey(conceptId)) {
                throw BusinessException.notFound("知识点不存在");
            }
        }

        // 3) 有界 SQL：过滤 + 倒序 + LIMIT 一次完成。
        List<LearningEvidence> evidences = evidenceMapper.selectRecentByConcepts(
                userId, spaceId, ids, normalizeRecentLimit(limit));
        if (evidences.isEmpty()) {
            return List.of();
        }

        // 4) 一次 IN 查询加载全部 sources，再按 evidenceId 分组；禁止逐条 loadSources。
        List<Long> evidenceIds = evidences.stream()
                .map(LearningEvidence::getId)
                .filter(Objects::nonNull)
                .toList();
        Map<Long, List<EvidenceSourceResponse>> sourcesByEvidence = evidenceIds.isEmpty()
                ? Map.of()
                : evidenceSourceMapper.selectByEvidenceIds(evidenceIds).stream()
                        .filter(source -> source.getEvidenceId() != null)
                        .collect(Collectors.groupingBy(
                                EvidenceSource::getEvidenceId,
                                LinkedHashMap::new,
                                Collectors.mapping(
                                        EvidenceSourceResponse::from,
                                        Collectors.toList())));
        return evidences.stream()
                .map(evidence -> EvidenceResponse.from(
                        evidence,
                        sourcesByEvidence.getOrDefault(evidence.getId(), List.of())))
                .toList();
    }

    private static List<Long> normalizeConceptIds(List<Long> requested) {
        if (requested == null || requested.isEmpty()) {
            throw new BusinessException(400, "conceptIds 不能为空");
        }
        LinkedHashSet<Long> unique = new LinkedHashSet<>();
        for (Long conceptId : requested) {
            if (conceptId == null || conceptId <= 0) {
                throw new BusinessException(400, "conceptIds 必须为正整数");
            }
            unique.add(conceptId);
        }
        if (unique.size() > MAX_CONCEPTS) {
            throw new BusinessException(400, "一次最多查询 " + MAX_CONCEPTS + " 个知识点");
        }
        return List.copyOf(unique);
    }

    private static int normalizeRecentLimit(Integer requested) {
        if (requested == null) {
            return DEFAULT_RECENT_LIMIT;
        }
        return Math.max(1, Math.min(requested, MAX_RECENT_LIMIT));
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
            if (sourceExists(evidenceId, ref)) {
                continue;
            }
            EvidenceSource source = new EvidenceSource();
            source.setEvidenceId(evidenceId);
            source.setSourceType(ref.sourceType());
            source.setSourceId(ref.sourceId());
            try {
                evidenceSourceMapper.insert(source);
            } catch (DuplicateKeyException e) {
                // 只有「这行来源确实已经被并发事务插入」才允许吞掉。
                // 不能把所有 DuplicateKeyException 都当成幂等：如果冲突来自别的约束
                // （或行随后又消失），继续执行会掩盖真实的数据问题。
                if (!sourceExists(evidenceId, ref)) {
                    throw e;
                }
                log.debug("Evidence 来源并发命中唯一键, evidenceId={}, sourceType={}, sourceId={}",
                        evidenceId, ref.sourceType(), ref.sourceId());
            }
        }
    }

    private boolean sourceExists(Long evidenceId, SourceRef ref) {
        Long already = evidenceSourceMapper.selectCount(new LambdaQueryWrapper<EvidenceSource>()
                .eq(EvidenceSource::getEvidenceId, evidenceId)
                .eq(EvidenceSource::getSourceType, ref.sourceType())
                .eq(EvidenceSource::getSourceId, ref.sourceId()));
        return already != null && already > 0;
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
