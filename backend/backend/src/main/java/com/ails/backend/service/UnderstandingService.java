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
import com.ails.backend.model.UnderstandingProposalDraft;
import com.ails.backend.mapper.LearningUnderstandingMapper;
import com.ails.backend.mapper.UnderstandingRevisionMapper;
import com.ails.backend.utils.JsonUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
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
        return loadOrCreateLocked(userId, conceptId);
    }

    /**
     * 加锁读快照，没有就插入。命名为 locked 是提醒：<b>它必须在调用方已开启的事务里跑</b>
     * （{@code FOR UPDATE} 脱离事务没有意义）。
     * <p>
     * 顺序刻意是「先加锁读 → 判断 → 插入」，而不是「先普通读 → 判断 → 加锁」：
     * InnoDB 的一致性读视图在事务里第一次<b>普通</b> SELECT 时建立，先普通读会把视图钉在旧时刻，
     * 等锁到手后看到的还是旧数据。详见
     * {@link com.ails.backend.mapper.LearningUnderstandingMapper#selectByUserAndConceptForUpdate}。
     * <p>
     * 并发下的两条路径都要处理：唯一索引上等值条件的 gap 锁<b>不互斥</b>，所以
     * 「两边都没查到、都去 INSERT」是可能的，输的那个靠 DuplicateKeyException 兜住。
     */
    private LearningUnderstanding loadOrCreateLocked(Long userId, Long conceptId) {
        LearningUnderstanding existing = understandingMapper.selectByUserAndConceptForUpdate(userId, conceptId);
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
        try {
            understandingMapper.insert(created);
            return created;
        } catch (DuplicateKeyException e) {
            // 另一个事务先插进去了。重取一次加锁读 —— 加锁读永远读最新已提交版本，
            // 所以这里能看到对方写好的行，即使本事务的一致性读视图还是旧的。
            LearningUnderstanding winner = understandingMapper.selectByUserAndConceptForUpdate(userId, conceptId);
            if (winner == null) {
                throw e; // 不是因为并发插入撞的唯一键，别吞
            }
            return winner;
        }
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
        // 走私有方法而不是 this.getOrCreate()：同类内部调用不经过 Spring 代理，那上面的
        // @Transactional 是失效的 —— 这里本来就该复用外层事务，与其留一个「看起来在生效、
        // 其实没生效」的注解，不如直接调不加注解的内部方法，语义和事实一致。
        // 这一步同时把快照行的写锁拿到手，后面整段读改写都在锁内。
        LearningUnderstanding understanding = loadOrCreateLocked(userId, conceptId);

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

    /**
     * 用 Agent 的增量提案推进当前理解快照。
     * <p>
     * 与 {@link #apply} 的分工：
     * <ul>
     *   <li>{@code apply} 处理<b>一条证据</b>：把正确/错误观察映射成 known / misconception，
     *       误区走 {@link MisconceptionLifecycle} 的完整迁移；</li>
     *   <li>{@code applyProposal} 处理<b>Agent 的合并建议</b>：known / uncertain 直接去重合并，
     *       新增误区按「一次错误证据」处理（ACTIVE），但不负责解除 —— 解除仍必须由足够的
     *       反向正确证据驱动。</li>
     * </ul>
     * 两条路径都遵守同一条铁律：<b>只有内容真的变化才写 Revision</b>。
     * 否则 Agent 重放同一份提案会不断产生内容相同的版本，Revision 历史就失去意义。
     *
     * @param triggerEvidenceId 触发本次变化的 Evidence，可为 null（纯提案变化）
     * @return 更新后的快照与本次 Revision；内容无变化时 revision 为 null；
     *         proposal 为空时返回 null（连快照都不创建）
     */
    @Transactional
    public ApplyResult applyProposal(Long userId, Long conceptId,
                                     UnderstandingProposalDraft proposal, Long triggerEvidenceId) {
        if (proposal == null || proposal.isEmpty()) {
            return null;
        }
        LearningUnderstanding understanding = loadOrCreateLocked(userId, conceptId);

        String beforeSummary = understanding.getSummary();
        String beforeKnown = understanding.getKnownParts();
        String beforeMisconceptions = understanding.getMisconceptions();
        String beforeUncertain = understanding.getUncertainParts();
        String beforeExplanation = understanding.getLatestUserExplanation();
        UnderstandingStatus beforeStatus = understanding.getStatus();

        List<String> known = new ArrayList<>(JsonUtil.toStringList(understanding.getKnownParts()));
        List<String> uncertain = new ArrayList<>(JsonUtil.toStringList(understanding.getUncertainParts()));
        proposal.knownParts().forEach(item -> addDistinct(known, item));
        proposal.uncertainParts().forEach(item -> addDistinct(uncertain, item));

        // 误区不是只增不减的列表：这里只负责「新增 / 重新激活」，
        // 解除必须由 apply() 里足够数量的反向证据完成。
        List<Misconception> misconceptions =
                new ArrayList<>(MisconceptionLifecycle.parse(understanding.getMisconceptions()));
        LocalDateTime at = LocalDateTime.now();
        proposal.misconceptions().forEach(text -> applyProposedMisconception(misconceptions, text, at));

        understanding.setKnownParts(JsonUtil.fromStringList(known));
        understanding.setUncertainParts(JsonUtil.fromStringList(uncertain));
        understanding.setMisconceptions(MisconceptionLifecycle.serialize(misconceptions));
        understanding.setSummary(buildSummary(known, misconceptions, uncertain));
        if (StringUtils.hasText(proposal.latestUserExplanation())) {
            understanding.setLatestUserExplanation(proposal.latestUserExplanation());
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
        revision.setTriggerEvidenceId(triggerEvidenceId);
        revisionMapper.insert(revision);

        understanding.setCurrentRevisionId(revision.getId());
        understandingMapper.updateById(understanding);
        return new ApplyResult(understanding, revision);
    }

    /**
     * 有证据但 Agent 没有给出理解提案时的最小推进：{@code UNKNOWN → EXPOSED}。
     * <p>
     * 状态语义里 EXPOSED 表示「接触过，但还判断不了理解程度」。用户针对某个概念提问、
     * 或者 Agent 只记录了一条观察而没有形成判断时，证据已经证明他碰过这个概念；
     * 如果什么都不做，概念会一直停在 UNKNOWN，学习状态页上看起来像从未出现。
     * <p>
     * <b>只做 UNKNOWN → EXPOSED 这一格。</b>更强的状态（PARTIAL / DEMONSTRATED /
     * MISCONCEPTION）必须由 Agent 的 proposal 或测试结果给出 —— 仅凭「提到过」就推断
     * 他理解了（或误解了）是在编造判断。
     * <p>
     * 已经是 EXPOSED 或更靠后的状态时直接返回，不产生 Revision：
     * 每一轮谈同一个概念都会新增证据，但理解并没有因此变化。
     */
    @Transactional
    public ApplyResult markExposed(Long userId, Long conceptId, Long triggerEvidenceId) {
        LearningUnderstanding understanding = loadOrCreateLocked(userId, conceptId);
        if (understanding.getStatus() != UnderstandingStatus.UNKNOWN) {
            return new ApplyResult(understanding, null);
        }
        understanding.setStatus(UnderstandingStatus.EXPOSED);
        understandingMapper.updateById(understanding);

        UnderstandingRevision revision = new UnderstandingRevision();
        revision.setUnderstandingId(understanding.getId());
        revision.setRevisionNo(nextRevisionNo(understanding.getId()));
        revision.setSummary(understanding.getSummary());
        revision.setKnownParts(understanding.getKnownParts());
        revision.setMisconceptions(understanding.getMisconceptions());
        revision.setUncertainParts(understanding.getUncertainParts());
        revision.setUserExplanation(understanding.getLatestUserExplanation());
        revision.setStatus(UnderstandingStatus.EXPOSED);
        revision.setTriggerEvidenceId(triggerEvidenceId);
        revisionMapper.insert(revision);

        understanding.setCurrentRevisionId(revision.getId());
        understandingMapper.updateById(understanding);
        return new ApplyResult(understanding, revision);
    }

    private static void applyProposedMisconception(List<Misconception> list, String text, LocalDateTime at) {
        String normalized = text.trim().replaceAll("\\s+", " ");
        for (int i = 0; i < list.size(); i++) {
            Misconception existing = list.get(i);
            if (!normalized.equals(existing.text())) {
                continue;
            }
            // 再次出现同一条误区：回到 ACTIVE。之前累计的反向确认必须清空 ——
            // 那些确认针对的是「旧一轮还没解决的状态」，不能拿来抵扣这一次的错误。
            list.set(i, new Misconception(
                    normalized,
                    MisconceptionStatus.ACTIVE,
                    existing.firstDetectedAt() == null ? at : existing.firstDetectedAt(),
                    at,
                    List.of()));
            return;
        }
        list.add(new Misconception(normalized, MisconceptionStatus.ACTIVE, at, at, List.of()));
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

    /**
     * 下一个 revision_no。
     * <p>
     * 必须走<b>当前读</b>（{@code MAX(...) FOR UPDATE}），不能走普通 COUNT：
     * 写入方虽然都持有理解快照的行锁，但分析事务在拿到这把锁之前往往已经执行过普通
     * SELECT（读证据、读概念），一致性读视图就此固定。那个视图可能早于对方提交的
     * Revision，普通 COUNT 会少算一条，两个事务便会算出同一个号去撞
     * {@code uk_revision_no}。加锁读不受视图影响，永远看最新已提交数据。
     * <p>
     * Revision 没有软删、也没有删除入口，所以 MAX 与 COUNT 等价，不需要处理空洞。
     */
    private Integer nextRevisionNo(Long understandingId) {
        Integer max = revisionMapper.selectMaxRevisionNoForUpdate(understandingId);
        return (max == null ? 0 : max) + 1;
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
