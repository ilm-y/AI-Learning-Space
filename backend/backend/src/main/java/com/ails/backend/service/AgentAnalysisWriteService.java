package com.ails.backend.service;

import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.TestTask;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.VerificationAction;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.model.MemoryCandidateDraft;
import com.ails.backend.model.UnderstandingProposalDraft;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 把一份 Agent 分析结果写进数据库 —— <b>整个分析链路上唯一接触写操作的地方</b>。
 * <p>
 * Python Agent 不连 MySQL，只返回候选。本类在<b>一个事务</b>里按固定顺序落库，
 * 顺序与 {@link LearningAnalysisService} 的本地规则链路保持一致：
 * <pre>
 *   1. Evidence Upsert（append-only + dedupe）
 *   2. Understanding Proposal → Revision
 *   3. LearningState Projection
 *   4. Memory Reconcile
 *   5. Verification（TestTask + 对话里的题卡消息）
 * </pre>
 * 任一步失败都整体回滚，由调用方退回 {@code rules-v1}；不会留下「证据写了、理解没更新」
 * 这种半完成状态。
 *
 * <h3>为什么不再调用 {@link UnderstandingService#apply}</h3>
 * {@code apply} 是本地规则版的映射（CORRECT → knownParts、INCORRECT → 误区）。
 * 接入 Agent 后，<b>理解如何演进由 Agent 的 proposal 决定</b>，Evidence 只是被记录的观察。
 * 两条路径同时跑会把同一个观察计算两遍，产生一次假的理解变化。
 * <p>
 * 但误区不能因此失去生命周期：{@code applyProposal} 会先用本轮证据推进已有误区
 * （INCORRECT 新建 / 高质量 CORRECT 解除），再合并提案；没有提案的概念走
 * {@link UnderstandingService#evolveMisconceptions}。Agent 仍不能凭一次输出
 * 把误区「说没了」—— 解除必须由独立的反向证据驱动。
 *
 * <h3>为什么先集中校验 conceptId</h3>
 * conceptId 来自模型输出，不能假设它一定属于当前学习空间。这里一次查出空间下的
 * 全部知识点做内存校验，而不是对每条候选调用抛异常的 {@code accessibleConcept} ——
 * 后者一旦在事务内抛出并被捕获，Spring 会把共享事务标记成 rollback-only，
 * 后面的写入全部失败。非法候选只跳过并记日志。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class AgentAnalysisWriteService {

    /** 与 learning_evidence.observation 的使用约定一致：一条观察最多 200 字。 */
    private static final int OBSERVATION_MAX_LENGTH = 200;

    private final EvidenceService evidenceService;
    private final UnderstandingService understandingService;
    private final LearningStateService learningStateService;
    private final MemoryService memoryService;
    private final VerificationService verificationService;
    private final ConceptMapper conceptMapper;

    /**
     * 一次分析写入需要的上下文。
     *
     * @param learningSpaceId 当前对话所在空间，用于校验 Agent 返回的 conceptId
     * @param messageId       本轮用户消息，Evidence 的主来源；为 null 时无法写证据
     * @param agentRunId      本次 ANALYSIS run，写进 Evidence 的 first/last_seen_run_id
     */
    public record TurnContext(
            Long userId,
            Long conversationId,
            Long learningSpaceId,
            Long messageId,
            Long agentRunId) {
    }

    /** 写入结果。摘要直接用于 {@code agent_run.output_summary}。 */
    public record WriteResult(
            List<LearningEvidence> evidences,
            int proposalsApplied,
            int memoriesAdded,
            int memoriesUpdated,
            Long verificationTestId) {

        public String summary() {
            return "evidence=" + evidences.size()
                    + ", proposal=" + proposalsApplied
                    + ", memory+=" + memoriesAdded
                    + ", memory~=" + memoriesUpdated
                    + ", test=" + (verificationTestId == null ? "-" : verificationTestId);
        }
    }

    @Transactional
    public WriteResult apply(TurnContext context, AnalyzeAgentResult result) {
        if (context == null || result == null) {
            return new WriteResult(List.of(), 0, 0, 0, null);
        }
        Map<Long, Concept> concepts = conceptsOfSpace(context.learningSpaceId());

        List<LearningEvidence> written = new ArrayList<>();
        Map<Long, LearningEvidence> latestEvidence = new LinkedHashMap<>();
        // 按概念分组的两份中间产物，供 writeProposal 使用：
        // - evidences：同一概念的多条证据要一起推进误区生命周期；
        // - explanations：Agent 在证据上带的用户原话，不能因为「没有提案」而丢掉。
        Map<Long, List<LearningEvidence>> evidencesByConcept = new LinkedHashMap<>();
        Map<Long, String> userExplanations = new LinkedHashMap<>();
        writeEvidence(context, result, concepts, written, latestEvidence,
                evidencesByConcept, userExplanations);

        Map<Long, LearningUnderstanding> proposals = new LinkedHashMap<>();
        int proposalsApplied = writeProposal(context, result, concepts, latestEvidence,
                evidencesByConcept, userExplanations, proposals);

        projectState(context, written, proposals, latestEvidence);

        int[] memories = reconcileMemories(context, result, concepts);
        Long testId = applyVerification(context, result, concepts);

        WriteResult outcome = new WriteResult(
                List.copyOf(written), proposalsApplied, memories[0], memories[1], testId);
        log.debug("agent 分析写入完成, userId={}, runId={}, {}",
                context.userId(), context.agentRunId(), outcome.summary());
        return outcome;
    }

    // ================= 1. Evidence =================

    /**
     * 逐条追加证据。命中 dedupe_key 的候选不会产生新行，也不会进入后续的状态投影 ——
     * 同一条观察重复出现时，理解本来就不该再变一次。
     */
    private void writeEvidence(TurnContext context, AnalyzeAgentResult result,
                               Map<Long, Concept> concepts,
                               List<LearningEvidence> written,
                               Map<Long, LearningEvidence> latestEvidence,
                               Map<Long, List<LearningEvidence>> evidencesByConcept,
                               Map<Long, String> userExplanations) {
        if (context.messageId() == null) {
            // Evidence 必须有来源；没有消息 id 说明这条分析是离线触发的，
            // 此时宁可不写证据，也不能编一个来源出来。
            log.warn("跳过证据写入：缺少 messageId, userId={}, runId={}",
                    context.userId(), context.agentRunId());
            return;
        }
        for (AnalyzeAgentResult.EvidenceCandidate candidate : result.evidenceCandidates()) {
            if (candidate == null || !concepts.containsKey(candidate.conceptId())) {
                log.warn("跳过证据候选：conceptId 不属于当前空间, userId={}, conceptId={}",
                        context.userId(), candidate == null ? null : candidate.conceptId());
                continue;
            }
            String observation = truncate(candidate.observation(), OBSERVATION_MAX_LENGTH);
            if (!StringUtils.hasText(observation)) {
                continue;
            }
            Long conceptId = candidate.conceptId();
            // 快照必须先存在：Evidence 要挂 understandingId
            LearningUnderstanding understanding =
                    understandingService.getOrCreate(context.userId(), conceptId);

            EvidenceService.AppendResult appended = evidenceService.append(new EvidenceService.Draft(
                    context.userId(),
                    conceptId,
                    understanding.getId(),
                    candidate.evidenceType() == null ? EvidenceType.OBSERVATION : candidate.evidenceType(),
                    observation,
                    candidate.correctness() == null ? EvidenceCorrectness.UNKNOWN : candidate.correctness(),
                    candidate.reasoningQuality(),
                    candidate.specificity(),
                    candidate.aiConfidence(),
                    candidate.needFollowUp(),
                    context.agentRunId(),
                    result.analyzerVersion(),
                    List.of(new EvidenceService.SourceRef(EvidenceSourceType.MESSAGE, context.messageId()))));
            if (!appended.created()) {
                continue;
            }
            LearningEvidence evidence = appended.evidence();
            written.add(evidence);
            // 同一概念的多条证据里，保留最后一条作为 Revision 的 trigger：
            // 它是本轮最新的观察，指向它比指向第一条更符合「这次变化由什么触发」。
            latestEvidence.put(conceptId, evidence);
            evidencesByConcept.computeIfAbsent(conceptId, key -> new ArrayList<>()).add(evidence);
            // 用户自己的原话是 Understanding 最强的信号，必须留存：
            // 提案可能只覆盖一个概念，甚至完全为空，不能只靠 proposal 那条路。
            if (StringUtils.hasText(candidate.userExplanation())) {
                userExplanations.putIfAbsent(conceptId, candidate.userExplanation().trim());
            }
        }
    }

    // ================= 2. Understanding Proposal =================

    private int writeProposal(TurnContext context, AnalyzeAgentResult result,
                              Map<Long, Concept> concepts,
                              Map<Long, LearningEvidence> latestEvidence,
                              Map<Long, List<LearningEvidence>> evidencesByConcept,
                              Map<Long, String> userExplanations,
                              Map<Long, LearningUnderstanding> proposals) {
        int applied = 0;
        // 已经被提案（或原话）处理过的概念；它们不再走下面的「纯误区演进」分支。
        Set<Long> covered = new LinkedHashSet<>();

        AnalyzeAgentResult.UnderstandingProposal proposal = result.understandingProposal();
        if (proposal != null && proposal.conceptId() != null) {
            Long conceptId = proposal.conceptId();
            if (!concepts.containsKey(conceptId)) {
                log.warn("跳过理解提案：conceptId 不属于当前空间, userId={}, conceptId={}",
                        context.userId(), conceptId);
            } else {
                // 提案没带用户原话时，用证据里的那句话补上：两者是同一轮的用户表达，
                // 但只有写入快照的这一路能把它带到下一轮上下文。
                String explanation = StringUtils.hasText(proposal.latestUserExplanation())
                        ? proposal.latestUserExplanation()
                        : userExplanations.get(conceptId);
                UnderstandingProposalDraft draft = new UnderstandingProposalDraft(
                        proposal.knownParts(),
                        proposal.uncertainParts(),
                        proposal.misconceptions(),
                        explanation);
                if (!draft.isEmpty()) {
                    UnderstandingService.ApplyResult appliedResult = understandingService.applyProposal(
                            context.userId(), conceptId, draft,
                            evidencesByConcept.getOrDefault(conceptId, List.of()),
                            evidenceId(latestEvidence.get(conceptId)));
                    if (appliedResult != null) {
                        proposals.put(conceptId, appliedResult.understanding());
                        covered.add(conceptId);
                        // revision == null 表示与当前快照完全一致，不该记成一次「应用了提案」
                        if (appliedResult.revision() != null) {
                            applied++;
                        }
                    }
                }
            }
        }

        // 提案只覆盖一个概念，而本轮证据可能提到多个。没被覆盖的用户原话单独落一条，
        // 不能因为「主提案在别的概念上」就把这句话丢掉。
        for (Map.Entry<Long, String> entry : userExplanations.entrySet()) {
            if (covered.contains(entry.getKey())) {
                continue;
            }
            UnderstandingProposalDraft draft = new UnderstandingProposalDraft(
                    List.of(), List.of(), List.of(), entry.getValue());
            UnderstandingService.ApplyResult appliedResult = understandingService.applyProposal(
                    context.userId(), entry.getKey(), draft,
                    evidencesByConcept.getOrDefault(entry.getKey(), List.of()),
                    evidenceId(latestEvidence.get(entry.getKey())));
            if (appliedResult != null) {
                proposals.put(entry.getKey(), appliedResult.understanding());
                covered.add(entry.getKey());
                if (appliedResult.revision() != null) {
                    applied++;
                }
            }
        }

        // 既没有提案、也没有用户原话的概念：仍要按证据推进误区生命周期
        // （INCORRECT 新建 ACTIVE、足够强的 CORRECT 解除），否则 Agent 路径的误区只增不减。
        for (Map.Entry<Long, List<LearningEvidence>> entry : evidencesByConcept.entrySet()) {
            if (covered.contains(entry.getKey())) {
                continue;
            }
            UnderstandingService.ApplyResult evolved = understandingService.evolveMisconceptions(
                    context.userId(), entry.getKey(), entry.getValue(),
                    evidenceId(latestEvidence.get(entry.getKey())));
            if (evolved != null) {
                proposals.put(entry.getKey(), evolved.understanding());
            }
        }
        return applied;
    }

    // ================= 3. Learning State =================

    /**
     * 每个被触碰过的概念投影一次，而不是每条证据投影一次。
     * <p>
     * 投影是幂等的物化视图，重复调用不会错，但会在同一事务里多做几次加锁读；
     * 更重要的是，一次分析里「证据 + 提案」应该被看成同一个新的理解状态。
     * <p>
     * 正常情况下 {@link #writeProposal} 已经保证「每个有证据的概念都有快照」：
     * 提案合并、原话落库、纯误区演进三条路都会返回 ApplyResult，并让状态推进到
     * EXPOSED（证据证明用户接触过，只是 Agent 还没形成判断）。
     * {@link UnderstandingService#markExposed} 保留为兜底，覆盖将来新增写入分支时
     * 「证据写了但理解没被推进」的情况。
     *
     * @param proposals 有提案的概念 → 提案应用后的快照（可能没有 Revision，但快照仍是最新的）
     */
    private void projectState(TurnContext context, List<LearningEvidence> written,
                              Map<Long, LearningUnderstanding> proposals,
                              Map<Long, LearningEvidence> latestEvidence) {
        Set<Long> touched = new LinkedHashSet<>();
        written.forEach(e -> touched.add(e.getConceptId()));
        touched.addAll(proposals.keySet());

        for (Long conceptId : touched) {
            LearningEvidence latest = latestEvidence.get(conceptId);
            LearningUnderstanding understanding = proposals.get(conceptId);
            if (understanding == null && latest != null) {
                understanding = understandingService
                        .markExposed(context.userId(), conceptId, latest.getId())
                        .understanding();
            }
            if (understanding == null) {
                understanding = understandingService.find(context.userId(), conceptId);
            }
            if (understanding == null) {
                // 理论上不会发生（写过证据的概念一定先 getOrCreate 过），
                // 但投影不是关键路径，缺快照时跳过好过抛异常把整份分析回滚。
                continue;
            }
            learningStateService.project(context.userId(), conceptId, understanding,
                    latest == null ? null : latest.getCreatedAt());
        }
    }

    // ================= 4. Memory =================

    /** @return {@code [added, updated]} */
    private int[] reconcileMemories(TurnContext context, AnalyzeAgentResult result,
                                    Map<Long, Concept> concepts) {
        int added = 0;
        int updated = 0;
        for (AnalyzeAgentResult.MemoryCandidate candidate : result.memoryCandidates()) {
            if (candidate == null) {
                continue;
            }
            MemoryCandidateDraft draft = new MemoryCandidateDraft(
                    candidate.scopeType(),
                    candidate.learningSpaceId(),
                    candidate.conceptId(),
                    candidate.memoryType(),
                    candidate.memoryKey(),
                    candidate.content(),
                    candidate.importance(),
                    candidate.action());
            if (!draft.isWritable() || !scopeAllowed(draft, context, concepts)) {
                log.warn("跳过记忆候选：作用域不合法, userId={}, scope={}, conceptId={}",
                        context.userId(), draft.scopeType(), draft.conceptId());
                continue;
            }
            MemoryService.ReconcileOutcome outcome = memoryService.reconcileCandidate(
                    context.userId(), draft, MemorySourceType.MESSAGE, context.messageId());
            switch (outcome) {
                case ADDED -> added++;
                case UPDATED -> updated++;
                case SKIPPED -> {
                    // 已存在 / 已归档 / 并发写入，都属于正常的收敛结果，不需要额外处理
                }
            }
        }
        return new int[]{added, updated};
    }

    /**
     * 只允许把记忆写在「当前对话能看到的地方」：GLOBAL、当前空间、当前空间下的知识点。
     * 模型给出别的空间 id 时直接丢弃 —— 那会让这条记忆出现在另一个学习空间的检索结果里。
     */
    private boolean scopeAllowed(MemoryCandidateDraft draft, TurnContext context,
                                 Map<Long, Concept> concepts) {
        return switch (draft.scopeType()) {
            case GLOBAL -> true;
            case SPACE -> Objects.equals(draft.learningSpaceId(), context.learningSpaceId());
            case CONCEPT -> concepts.containsKey(draft.conceptId())
                    && (draft.learningSpaceId() == null
                    || Objects.equals(draft.learningSpaceId(), context.learningSpaceId()));
        };
    }

    // ================= 5. Verification =================

    private Long applyVerification(TurnContext context, AnalyzeAgentResult result,
                                   Map<Long, Concept> concepts) {
        AnalyzeAgentResult.VerificationDecision decision = result.verificationDecision();
        if (decision == null || decision.action() == null) {
            return null;
        }
        if (decision.action() == VerificationAction.ASK_LATER) {
            // 目前没有调度器：把「稍后问」落成一条永远不会被处理的 pending 任务只会污染列表。
            // 这类意图由 Evidence 的 need_follow_up 承载（Agent 的候选证据自带该标记）。
            log.info("验证决策 ASK_LATER（仅记录）, userId={}, conceptId={}, trigger={}",
                    context.userId(), decision.conceptId(), decision.trigger());
            return null;
        }
        if (decision.action() != VerificationAction.ASK_NOW) {
            return null;
        }
        if (!concepts.containsKey(decision.conceptId())) {
            log.warn("跳过验证任务：conceptId 不属于当前空间, userId={}, conceptId={}",
                    context.userId(), decision.conceptId());
            return null;
        }
        TestTask task = verificationService.createFromAnalysis(new VerificationService.VerificationDraft(
                context.userId(),
                context.conversationId(),
                decision.conceptId(),
                decision.questionType(),
                decision.questionText(),
                decision.correctAnswer(),
                decision.trigger(),
                decision.reason()));
        return task == null ? null : task.getId();
    }

    // ================= 内部工具 =================

    private Map<Long, Concept> conceptsOfSpace(Long learningSpaceId) {
        if (learningSpaceId == null) {
            return Map.of();
        }
        return conceptMapper.selectList(new LambdaQueryWrapper<Concept>()
                        .eq(Concept::getLearningSpaceId, learningSpaceId))
                .stream()
                .collect(Collectors.toMap(Concept::getId, c -> c, (a, b) -> a, LinkedHashMap::new));
    }

    /** 证据可能不存在（例如这一轮没有新证据），Revision 的 trigger 允许为空。 */
    private static Long evidenceId(LearningEvidence evidence) {
        return evidence == null ? null : evidence.getId();
    }

    /** 观察文本截断；null 原样返回，由调用方判断是否为空。 */
    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        String trimmed = text.trim();
        return trimmed.length() <= max ? trimmed : trimmed.substring(0, max) + "…";
    }
}
