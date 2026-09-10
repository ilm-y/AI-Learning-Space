package com.ails.backend.service;

import com.ails.backend.dto.understanding.UnderstandingResponse;
import com.ails.backend.dto.understanding.UnderstandingRevisionResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.UnderstandingRevision;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.MisconceptionStatus;
import com.ails.backend.enums.UnderstandingStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.model.Misconception;
import com.ails.backend.model.MisconceptionLifecycle;
import com.ails.backend.mapper.LearningUnderstandingMapper;
import com.ails.backend.mapper.UnderstandingRevisionMapper;
import com.ails.backend.utils.JsonUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;

/**
 * 理解快照的唯一维护者。
 * <p>
 * <b>写入语义：可变的当前快照 + 不可变的历史。</b>
 * {@link LearningUnderstanding} 只保存「现在的理解」，每次<b>真正发生变化</b>都必须写入一条
 * {@link UnderstandingRevision} 并改写 currentRevisionId。没有变化就不留 Revision，
 * 所以 Revision 的数量总是等于「理解真正演进过的次数」。
 * <p>
 * 理解只能通过新的 Evidence 演进：本类不提供「直接改快照」的对外接口，
 * Understanding / Revision 也没有任何删除入口。
 */
@Service
@RequiredArgsConstructor
public class UnderstandingService {

    /** 判定为「已演示」所需的最少已理解条目 */
    private static final int DEMONSTRATED_MIN_KNOWN_PARTS = 2;

    private final LearningUnderstandingMapper understandingMapper;
    private final UnderstandingRevisionMapper revisionMapper;
    private final ConceptMapper conceptMapper;
    private final ConceptService conceptService;

    /** 一次 apply 的结果：更新后的快照 + 本次产生的 Revision（没有实际变化时为 null） */
    public record ApplyResult(LearningUnderstanding understanding, UnderstandingRevision revision) {
    }

    /**
     * 取用户对某概念的理解快照，不存在则建一条 UNKNOWN 的空快照。
     * <p>
     * 必须在写入 Evidence 之前调用 —— Evidence 要挂 understandingId。
     * 初始快照本身不产生 Revision：Revision 只记录「变化」，第一条 Revision 由第一次
     * 真正改变理解的操作产生。
     */
    @Transactional
    public LearningUnderstanding getOrCreate(Long userId, Long conceptId) {
        LearningUnderstanding existing = find(userId, conceptId);
        if (existing != null) {
            return existing;
        }
        LearningUnderstanding created = new LearningUnderstanding();
        created.setUserId(userId);
        created.setConceptId(conceptId);
        created.setStatus(UnderstandingStatus.UNKNOWN);
        created.setKnownParts(JsonUtil.fromStringList(List.of()));
        created.setMisconceptions(MisconceptionLifecycle.serialize(List.of()));
        created.setUncertainParts(JsonUtil.fromStringList(List.of()));
        understandingMapper.insert(created);
        return created;
    }

    /**
     * 用一条新证据推进当前理解快照。
     * <p>
     * 映射规则（本地规则版；接入 Python Agent 后由 Agent 的分析结果替换）：
     * <pre>
     *   CORRECT   → 观察进入 knownParts，同时作为反向证据推进已有误区
     *   PARTIAL   → 观察进入 knownParts 与 uncertainParts
     *   INCORRECT → 交给 MisconceptionLifecycle 激活/刷新对应误区
     *   UNKNOWN   → 只表示「接触过」，不进入任何列表
     * </pre>
     * 状态由快照内容派生，而不是由单条证据直接决定，避免一条 INCORRECT 就把
     * 长期积累的理解历史抹掉。
     * <p>
     * 误区的 ACTIVE / PENDING / 解除由 {@link MisconceptionLifecycle} 决定 ——
     * 那是纯函数，规则集中在一处，也不需要为此改数据模型。
     *
     * @param userExplanation 可选：用户自己的原话（MY_UNDERSTANDING 批注 / 测试回答）
     */
    @Transactional
    public ApplyResult apply(Long userId, Long conceptId, LearningEvidence evidence, String userExplanation) {
        LearningUnderstanding understanding = getOrCreate(userId, conceptId);

        // 变更前快照，用于判断是否真的发生了变化
        String beforeSummary = understanding.getSummary();
        String beforeKnown = understanding.getKnownParts();
        String beforeMisconceptions = understanding.getMisconceptions();
        String beforeUncertain = understanding.getUncertainParts();
        String beforeExplanation = understanding.getLatestUserExplanation();
        UnderstandingStatus beforeStatus = understanding.getStatus();

        List<String> known = new ArrayList<>(JsonUtil.toStringList(understanding.getKnownParts()));
        List<String> uncertain = new ArrayList<>(JsonUtil.toStringList(understanding.getUncertainParts()));

        String observation = normalize(evidence.getObservation());
        EvidenceCorrectness correctness = evidence.getCorrectness();
        if (StringUtils.hasText(observation) && correctness != null) {
            switch (correctness) {
                case CORRECT -> addDistinct(known, observation);
                case PARTIAL -> {
                    addDistinct(known, observation);
                    addDistinct(uncertain, observation);
                }
                case INCORRECT, UNKNOWN -> {
                    // 误区不在这里处理，交给 MisconceptionLifecycle 走完整生命周期；
                    // UNKNOWN 只是「接触过」，不进入任何列表
                }
            }
        }

        // 误区是有生命周期的：ACTIVE → PENDING → 解除，所以它不是一个只增不减的字符串列表
        List<Misconception> misconceptions =
                MisconceptionLifecycle.evolve(MisconceptionLifecycle.parse(understanding.getMisconceptions()), evidence);

        understanding.setKnownParts(JsonUtil.fromStringList(known));
        understanding.setMisconceptions(MisconceptionLifecycle.serialize(misconceptions));
        understanding.setUncertainParts(JsonUtil.fromStringList(uncertain));
        understanding.setSummary(buildSummary(known, misconceptions, uncertain));
        if (StringUtils.hasText(userExplanation)) {
            understanding.setLatestUserExplanation(userExplanation.trim());
        }
        understanding.setStatus(resolveStatus(understanding, beforeStatus));

        boolean changed = !Objects.equals(beforeSummary, understanding.getSummary())
                || !Objects.equals(beforeKnown, understanding.getKnownParts())
                || !Objects.equals(beforeMisconceptions, understanding.getMisconceptions())
                || !Objects.equals(beforeUncertain, understanding.getUncertainParts())
                || !Objects.equals(beforeExplanation, understanding.getLatestUserExplanation())
                || beforeStatus != understanding.getStatus();

        if (!changed) {
            return new ApplyResult(understanding, null);
        }

        understandingMapper.updateById(understanding);

        UnderstandingRevision revision = new UnderstandingRevision();
        revision.setUnderstandingId(understanding.getId());
        revision.setRevisionNo(nextRevisionNo(understanding.getId()));
        revision.setSummary(understanding.getSummary());
        revision.setKnownParts(understanding.getKnownParts());
        revision.setMisconceptions(understanding.getMisconceptions());
        revision.setUncertainParts(understanding.getUncertainParts());
        revision.setUserExplanation(understanding.getLatestUserExplanation());
        revision.setStatus(understanding.getStatus());
        revision.setTriggerEvidenceId(evidence.getId());
        revisionMapper.insert(revision);

        understanding.setCurrentRevisionId(revision.getId());
        understandingMapper.updateById(understanding);

        return new ApplyResult(understanding, revision);
    }

    // ================= 查询 =================

    @Transactional(readOnly = true)
    public UnderstandingResponse get(Long userId, Long conceptId) {
        Concept concept = conceptService.accessibleConcept(userId, conceptId);
        return UnderstandingResponse.from(find(userId, conceptId), conceptName(concept));
    }

    /**
     * 某个学习空间下已经形成过理解快照的知识点。没有快照的 Concept 不在结果里 ——
     * 「还没碰过」和「碰过但没形成判断」是两回事。
     */
    @Transactional(readOnly = true)
    public List<UnderstandingResponse> listBySpace(Long userId, Long spaceId) {
        conceptService.listBySpace(userId, spaceId);
        List<Long> conceptIds = conceptMapper.selectList(new LambdaQueryWrapper<Concept>()
                        .eq(Concept::getLearningSpaceId, spaceId))
                .stream().map(Concept::getId).toList();
        if (conceptIds.isEmpty()) {
            return List.of();
        }
        Map<Long, String> names = conceptMapper.selectBatchIds(conceptIds).stream()
                .collect(Collectors.toMap(Concept::getId, this::conceptName));
        return understandingMapper.selectList(new LambdaQueryWrapper<LearningUnderstanding>()
                        .eq(LearningUnderstanding::getUserId, userId)
                        .in(LearningUnderstanding::getConceptId, conceptIds)
                        .orderByDesc(LearningUnderstanding::getUpdatedAt))
                .stream()
                .map(u -> UnderstandingResponse.from(u, names.get(u.getConceptId())))
                .toList();
    }

    @Transactional(readOnly = true)
    public List<UnderstandingRevisionResponse> revisions(Long userId, Long conceptId) {
        conceptService.accessibleConcept(userId, conceptId);
        LearningUnderstanding understanding = find(userId, conceptId);
        if (understanding == null) {
            return List.of();
        }
        return revisionMapper.selectList(new LambdaQueryWrapper<UnderstandingRevision>()
                        .eq(UnderstandingRevision::getUnderstandingId, understanding.getId())
                        .orderByAsc(UnderstandingRevision::getRevisionNo))
                .stream().map(UnderstandingRevisionResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public LearningUnderstanding find(Long userId, Long conceptId) {
        return understandingMapper.selectOne(new LambdaQueryWrapper<LearningUnderstanding>()
                .eq(LearningUnderstanding::getUserId, userId)
                .eq(LearningUnderstanding::getConceptId, conceptId));
    }

    // ================= 内部工具 =================

    /**
     * 状态派生规则。
     * <p>
     * 优先看误区（近期的错误理解必须显性暴露），其次看是否已形成可演示的理解。
     * <p>
     * 注意这里的 {@code misconceptions} 是<b>当前仍然成立</b>的误区：已经解除的误区
     * 早就从快照里移除了，所以一条老误区不会把概念永久钉死在 MISCONCEPTION 上。
     * 处于 PENDING 的误区仍然算 MISCONCEPTION（还没确认解除），只是 attention 优先级更低。
     */
    private UnderstandingStatus resolveStatus(LearningUnderstanding understanding, UnderstandingStatus previous) {
        List<String> known = JsonUtil.toStringList(understanding.getKnownParts());
        List<Misconception> misconceptions = MisconceptionLifecycle.parse(understanding.getMisconceptions());
        List<String> uncertain = JsonUtil.toStringList(understanding.getUncertainParts());

        if (MisconceptionLifecycle.hasUnresolved(misconceptions)) {
            return UnderstandingStatus.MISCONCEPTION;
        }
        if (known.size() >= DEMONSTRATED_MIN_KNOWN_PARTS && uncertain.isEmpty()) {
            return UnderstandingStatus.DEMONSTRATED;
        }
        if (!known.isEmpty()) {
            return UnderstandingStatus.PARTIAL;
        }
        // 有证据但还没形成判断：从「无法判断」推进到「接触过」
        if (previous == UnderstandingStatus.EXPOSED || previous == UnderstandingStatus.UNKNOWN) {
            return UnderstandingStatus.EXPOSED;
        }
        return previous == null ? UnderstandingStatus.UNKNOWN : previous;
    }

    private Integer nextRevisionNo(Long understandingId) {
        Long count = revisionMapper.selectCount(new LambdaQueryWrapper<UnderstandingRevision>()
                .eq(UnderstandingRevision::getUnderstandingId, understandingId));
        return (count == null ? 0 : count.intValue()) + 1;
    }

    private String buildSummary(List<String> known, List<Misconception> misconceptions, List<String> uncertain) {
        List<String> active = MisconceptionLifecycle.textsOf(misconceptions, MisconceptionStatus.ACTIVE);
        List<String> pending = MisconceptionLifecycle.textsOf(misconceptions, MisconceptionStatus.PENDING);
        if (known.isEmpty() && active.isEmpty() && pending.isEmpty() && uncertain.isEmpty()) {
            return null;
        }
        StringBuilder sb = new StringBuilder();
        if (!known.isEmpty()) {
            sb.append("已理解：").append(String.join("；", known));
        }
        if (!active.isEmpty()) {
            appendSection(sb, "存在误区：", active);
        }
        // 正在被纠正的误区单独说，UI 上不该和仍然成立的误区长一样
        if (!pending.isEmpty()) {
            appendSection(sb, "误区可能正在解除：", pending);
        }
        if (!uncertain.isEmpty()) {
            appendSection(sb, "待确认：", uncertain);
        }
        return sb.toString();
    }

    private void appendSection(StringBuilder sb, String title, List<String> items) {
        if (sb.length() > 0) {
            sb.append(' ');
        }
        sb.append(title).append(String.join("；", items));
    }

    private static void addDistinct(List<String> list, String value) {
        if (!list.contains(value)) {
            list.add(value);
        }
    }

    private static String normalize(String text) {
        return text == null ? null : text.trim().replaceAll("\\s+", " ");
    }

    private String conceptName(Concept concept) {
        if (concept == null) {
            return null;
        }
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }
}
