package com.ails.backend.service;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Highlight;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningSession;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.Message;
import com.ails.backend.entity.TestAnswer;
import com.ails.backend.entity.TestTask;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.enums.HighlightType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.ConversationMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

/**
 * 学习闭环的编排者：把一次观察（消息 / 批注 / 测试回答）跑成完整链路。
 * <p>
 * <b>固定顺序</b>（顺序本身就是语义，不能调换）：
 * <pre>
 *   1. getOrCreateUnderstanding(user, concept)   不存在则建 UNKNOWN 快照
 *   2. Evidence                                   append-only、查重
 *   3. Understanding Update                       当前快照
 *   4. INSERT UnderstandingRevision               历史版本
 *   5. UPDATE current_revision_id                 指向最新版本
 *   6. LearningState Projection                   物化投影
 * </pre>
 * 第 1 步必须在第 2 步之前 —— Evidence 要挂 understandingId，快照得先存在。
 * <p>
 * 本类只做编排，不自己判断「学到什么程度」：状态演进的规则在
 * {@link UnderstandingService}，投影规则在 {@link LearningStateService}。
 * <p>
 * <b>返回 0 条证据是完全正常的</b>：普通闲聊不涉及任何已知 Concept 时，
 * analyzeMessage 返回空列表，不写 Evidence、不动 Understanding。
 * <p>
 * 当前的「抽取」是本地规则版（按 Concept 名称匹配 + 批注类型映射），
 * 接入 Python Agent 后由 Agent 的分析结果替换，链路与写入语义不变。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class LearningAnalysisService {

    /** 本地规则分析器版本。只用于溯源 —— 去重由 Evidence 的 dedupe_key 决定，与版本号无关。 */
    public static final String ANALYZER_VERSION = "rules-v1";

    /** 观察文本入库前的截断长度 */
    private static final int OBSERVATION_MAX_LENGTH = 200;
    /** 一句话的截断长度，用于从消息中摘出提及该概念的句子 */
    private static final int SENTENCE_MAX_LENGTH = 160;

    private final EvidenceService evidenceService;
    private final UnderstandingService understandingService;
    private final LearningStateService learningStateService;
    private final SessionService sessionService;
    private final AgentRunMapper agentRunMapper;
    private final ConceptMapper conceptMapper;
    private final ConversationMapper conversationMapper;

    /**
     * 一轮对话开始时要先落定的两件事：这条消息提到了哪些 Concept，以及这条 Conversation
     * 属于哪个学习活动。
     * <p>
     * <b>必须在调用 Agent 之前执行</b>，这是本方法独立于 {@link #analyzeMessage} 的唯一原因：
     * Agent 的上下文里要带「本阶段学习目标」（session-design.md §一.4），而 Session 是懒创建的 ——
     * 一条<b>新建</b>的 Conversation 在发第一条消息时 {@code session_id} 还是 NULL。
     * 晚一步（比如放在回复生成完之后）会出现一个尴尬的结果：最需要继承上个 Session 目标的那一轮
     * （新开对话接着学同一个知识点）恰恰什么都带不上。
     * <p>
     * 返回的 matched 要一路带到 {@link #analyzeMessage(Message, List)}，不要让它在那里再匹配一次 ——
     * 两次匹配之间消息内容不会变，重算只会多一次全表查 Concept。
     *
     * @return 命中的 Concept（可能为空 = 闲聊）与归属的学习活动（可能为 null）；都不为 null 时才需要往下传
     */
    public TurnOpening beginTurn(Conversation conversation, String userContent) {
        if (conversation == null) {
            return new TurnOpening(List.of(), null);
        }
        List<Concept> matched = matchConcepts(conversation.getLearningSpaceId(), userContent);

        // 「第一条有效学习消息」在这里被定义为「首次抽出至少一个 Concept」——
        // 判据与下面「有没有证据」完全一致，不另立一套标准。
        //
        // 刻意在 matched 为空时也调用：这样 SessionResolver 的 NONE 分支（闲聊不产生学习活动）
        // 是条活路径，而不是一处永远不会被执行到的死代码。
        // conversation.sessionId 一旦有值，resolveForConversation 就是空操作。
        LearningSession session = sessionService.resolveForConversation(conversation,
                matched.stream().map(this::conceptName).toList());
        return new TurnOpening(matched, session);
    }

    /**
     * {@link #beginTurn} 的结果。
     *
     * @param matched 消息里命中的 Concept，顺序按 concept id 升序，可空
     * @param session 这条对话归属的学习活动；{@code null} 表示没有（闲聊，或空间缺失）
     */
    public record TurnOpening(List<Concept> matched, LearningSession session) {
    }

    /**
     * 分析一条消息。可能抽出 0 / 1 / N 条证据（每个被提到的 Concept 各一条）。
     * <p>
     * 自己重新匹配 Concept，并自行完成 Session 归属 —— 供「不走 Chat 那一跳」的调用方使用。
     * 从 ChatService 过来的话请用 {@link #analyzeMessage(Message, List)}，避免重复匹配。
     */
    @Transactional
    public List<LearningEvidence> analyzeMessage(Message message) {
        if (message == null || StringUtils.hasText(message.getContent()) == false) {
            return List.of();
        }
        Conversation conversation = conversationMapper.selectById(message.getConversationId());
        if (conversation == null) {
            return List.of();
        }
        return analyzeMessage(message, beginTurn(conversation, message.getContent()).matched());
    }

    /**
     * 分析一条消息，复用调用方已经算好的 Concept 匹配结果。
     *
     * @param matched {@link #beginTurn} 的返回值 —— 必须来自同一条消息，否则会抽出错误的证据
     */
    @Transactional
    public List<LearningEvidence> analyzeMessage(Message message, List<Concept> matched) {
        if (message == null || StringUtils.hasText(message.getContent()) == false || matched == null) {
            return List.of();
        }
        Conversation conversation = conversationMapper.selectById(message.getConversationId());
        if (conversation == null) {
            return List.of();
        }
        if (matched.isEmpty()) {
            // 闲聊：没有任何已知 Concept 被提到，不产生证据
            return List.of();
        }
        boolean looksLikeQuestion = message.getContent().contains("?") || message.getContent().contains("？");

        List<Extracted> items = matched.stream()
                .map(concept -> new Extracted(
                        concept.getId(),
                        sentenceContaining(message.getContent(), conceptName(concept)),
                        // 普通消息里的信号只是「观察到他在谈这个」，不是他自己给的解释
                        EvidenceType.OBSERVATION,
                        // 单纯「提到」不等于理解，只说明接触过（EXPOSED）
                        EvidenceCorrectness.UNKNOWN,
                        null, null, null,
                        looksLikeQuestion,
                        null,
                        EvidenceSourceType.MESSAGE,
                        message.getId()))
                .toList();

        return runAnalysis(conversation.getUserId(), conversation, AgentRunType.ANALYSIS,
                "analyze message#" + message.getId(), items);
    }

    /**
     * 分析一条批注。是否产生证据由批注类型决定：
     * <pre>
     *   MY_UNDERSTANDING        → 强证据（用户自己的解释）
     *   MISCONCEPTION_CANDIDATE → 候选误区
     *   QUESTION                → 不确定性证据，需要跟进
     *   IMPORTANT               → 普通标记，不产生证据
     *   CONCEPT_LINK            → 只建概念关联，不产生证据
     * </pre>
     */
    @Transactional
    public List<LearningEvidence> analyzeHighlight(Highlight highlight, Message message) {
        if (highlight == null || message == null) {
            return List.of();
        }
        Conversation conversation = conversationMapper.selectById(message.getConversationId());
        if (conversation == null) {
            return List.of();
        }

        EvidenceCorrectness correctness = switch (highlight.getMarkType()) {
            case MY_UNDERSTANDING -> EvidenceCorrectness.CORRECT;
            case MISCONCEPTION_CANDIDATE -> EvidenceCorrectness.INCORRECT;
            case QUESTION -> EvidenceCorrectness.UNKNOWN;
            case IMPORTANT, CONCEPT_LINK -> null;
        };
        if (correctness == null) {
            // 普通标记与概念关联不产生 Evidence
            return List.of();
        }

        String signal = (highlight.getSelectedText() == null ? "" : highlight.getSelectedText())
                + " " + (highlight.getNote() == null ? "" : highlight.getNote());
        List<Concept> matched = matchConcepts(conversation.getLearningSpaceId(), signal);
        if (matched.isEmpty()) {
            // 批注里没有点出任何已知 Concept，退回消息本身提到的 Concept
            matched = matchConcepts(conversation.getLearningSpaceId(), message.getContent());
        }
        if (matched.isEmpty()) {
            return List.of();
        }

        // 「用户自己的话」在哪一格，按批注类型分：
        // MY_UNDERSTANDING 的典型场景是「用户选中自己刚说的那句话，标成我的理解」——
        // 选中的正文才是他的解释，note 只是他另加的旁批（常常就是一句标签）。
        // 反过来优先 note，会把「我的理解」这种标签当成用户的说法存下来，
        // 而 Understanding.latestUserExplanation 正是要原样喂给下一轮 Agent 的那句话。
        // 其余类型维持原样：note 承载的是用户额外交代的判断依据，信息量更大。
        boolean selfExplanation = highlight.getMarkType() == HighlightType.MY_UNDERSTANDING;
        String observation = truncate(selfExplanation
                        ? firstNonBlank(highlight.getSelectedText(), highlight.getNote())
                        : firstNonBlank(highlight.getNote(), highlight.getSelectedText()),
                OBSERVATION_MAX_LENGTH);
        // 用户自己的解释是最强信号，单独留存为 latestUserExplanation
        String userExplanation = selfExplanation ? observation : null;

        EvidenceType evidenceType = switch (highlight.getMarkType()) {
            case MY_UNDERSTANDING -> EvidenceType.USER_EXPLANATION;
            case MISCONCEPTION_CANDIDATE -> EvidenceType.USER_CORRECTION;
            // QUESTION 与 IMPORTANT / CONCEPT_LINK 走不到这里（correctness 为 null 时已提前返回），
            // 剩下的 QUESTION 是一种「他自己标记的待跟进」，算批注信号而不是观察。
            default -> EvidenceType.HIGHLIGHT_SIGNAL;
        };

        List<Extracted> items = new ArrayList<>();
        for (Concept concept : matched) {
            items.add(new Extracted(
                    concept.getId(), observation, evidenceType, correctness,
                    highlight.getMarkType() == HighlightType.MY_UNDERSTANDING ? 0.8f : null,
                    null, null,
                    highlight.getMarkType() == HighlightType.QUESTION,
                    userExplanation,
                    EvidenceSourceType.HIGHLIGHT,
                    highlight.getId()));
        }

        return runAnalysis(highlight.getUserId(), conversation, AgentRunType.ANALYSIS,
                "analyze highlight#" + highlight.getId(), items);
    }

    /**
     * 分析一次测试回答。测试回答比普通聊天具有更高的验证价值，因此直接给出
     * CORRECT / INCORRECT 判定，是理解状态从 PARTIAL 走向 DEMONSTRATED（或暴露为
     * MISCONCEPTION）的主要途径。
     */
    @Transactional
    public List<LearningEvidence> analyzeTestAnswer(TestAnswer answer, TestTask task, EvidenceCorrectness correctness) {
        if (answer == null || task == null) {
            return List.of();
        }
        String observation = truncate(StringUtils.hasText(answer.getUserAnswer())
                ? answer.getUserAnswer() : task.getQuestionText(), OBSERVATION_MAX_LENGTH);

        Extracted item = new Extracted(
                task.getConceptId(), observation, EvidenceType.TEST_RESULT,
                correctness == null ? EvidenceCorrectness.UNKNOWN : correctness,
                // 测试回答是结构化验证，推理质量与把握度都高于自由聊天
                0.9f, null, 0.9f,
                correctness != EvidenceCorrectness.CORRECT,
                answer.getUserAnswer(),
                EvidenceSourceType.TEST_ANSWER,
                answer.getId());

        // 测试回答只带 conversation_id，取一次归属 —— 让这条 run 的 session_id 与 CHAT / ANALYSIS
        // 的写法一致，而不是「有的 run 有归属、有的没有」这种要靠猜的差异
        Conversation conversation = task.getConversationId() == null
                ? null : conversationMapper.selectById(task.getConversationId());

        return runAnalysis(task.getUserId(), conversation, AgentRunType.TEST,
                "analyze testAnswer#" + answer.getId(), List.of(item));
    }

    // ================= 编排 =================

    private record Extracted(
            Long conceptId,
            String observation,
            EvidenceType evidenceType,
            EvidenceCorrectness correctness,
            Float reasoningQuality,
            Float specificity,
            Float aiConfidence,
            Boolean needFollowUp,
            String userExplanation,
            EvidenceSourceType sourceType,
            Long sourceId) {
    }

    /**
     * 按固定顺序跑完整条链路，整段由一条 agent_run 串起来（用于溯源，不再是去重依据）。
     * <p>
     * 返回值只包含<b>本次真的新增</b>的 Evidence：命中 dedupe_key 的那些是「又一次看到同一条
     * 观察」，数据库里没有新行，快照也不会因此变化，所以它们既不进 written、也不触发
     * Understanding 投影 —— 否则每重跑一次分析就会多出一条内容完全相同的 Revision。
     */
    private List<LearningEvidence> runAnalysis(Long userId, Conversation conversation, AgentRunType runType,
                                               String inputSummary, List<Extracted> items) {
        if (items.isEmpty()) {
            return List.of();
        }
        LocalDateTime startedAt = LocalDateTime.now();
        AgentRun run = new AgentRun();
        run.setUserId(userId);
        // 归属照抄所属对话。对分析 run 来说这个字段回答的是「这条证据是在哪段学习活动里产生的」，
        // 而它可能为 null —— 对话还没被归入任何 Session，或用户在闲聊（Resolver 的 NONE 分支）。
        // 直接从 conversation 取而不是让调用方传 id：少一次查库，也不会两边写岔。
        run.setConversationId(conversation == null ? null : conversation.getId());
        run.setSessionId(conversation == null ? null : conversation.getSessionId());
        run.setRunType(runType);
        run.setStatus(AgentRunStatus.RUNNING);
        run.setInputSummary(truncate(inputSummary, 255));
        agentRunMapper.insert(run);

        List<LearningEvidence> written = new ArrayList<>();
        try {
            for (Extracted item : items) {
                // 1. 快照必须先存在，Evidence 才有 understandingId 可挂
                LearningUnderstanding understanding =
                        understandingService.getOrCreate(userId, item.conceptId());

                // 2. 追加证据（append-only + dedupe_key upsert）
                EvidenceService.AppendResult appended = evidenceService.append(new EvidenceService.Draft(
                        userId,
                        item.conceptId(),
                        understanding.getId(),
                        item.evidenceType(),
                        item.observation(),
                        item.correctness(),
                        item.reasoningQuality(),
                        item.specificity(),
                        item.aiConfidence(),
                        item.needFollowUp(),
                        run.getId(),
                        ANALYZER_VERSION,
                        List.of(new EvidenceService.SourceRef(item.sourceType(), item.sourceId()))));
                if (!appended.created()) {
                    // 这条观察之前已经记过（换一个 run 再看一遍也一样），跳到下一个
                    continue;
                }
                LearningEvidence evidence = appended.evidence();
                written.add(evidence);

                // 3~5. 更新快照 → 产生 Revision → 指向最新 Revision
                UnderstandingService.ApplyResult result = understandingService.apply(
                        userId, item.conceptId(), evidence, item.userExplanation());

                // 6. 投影到学习状态（唯一写入路径）
                learningStateService.project(userId, item.conceptId(),
                        result.understanding(), evidence.getCreatedAt());
            }

            run.setStatus(AgentRunStatus.COMPLETED);
            run.setOutputSummary("evidence=" + written.size());
        } catch (RuntimeException e) {
            run.setStatus(AgentRunStatus.FAILED);
            run.setErrorMessage(truncate(e.toString(), 1000));
            throw e;
        } finally {
            run.setFinishedAt(LocalDateTime.now());
            // 封顶后再收窄到 int：裸的 (int) toMillis() 超过 24.8 天会翻成负数（agent_run.duration_ms 是 INT）
            run.setDurationMs((int) Math.min(Integer.MAX_VALUE,
                    Duration.between(startedAt, run.getFinishedAt()).toMillis()));
            agentRunMapper.updateById(run);
        }
        return written;
    }

    // ================= 内部工具 =================

    /**
     * 在文本里按名称匹配当前学习空间下的 Concept。
     * <p>
     * 这是「本地规则版」的分析器：宁可少抽（匹配不到就不产生证据），也不瞎猜。
     */
    private List<Concept> matchConcepts(Long spaceId, String text) {
        if (spaceId == null || !StringUtils.hasText(text)) {
            return List.of();
        }
        List<Concept> concepts = conceptMapper.selectList(new LambdaQueryWrapper<Concept>()
                .eq(Concept::getLearningSpaceId, spaceId)
                .orderByAsc(Concept::getId));
        return concepts.stream()
                .filter(c -> text.contains(c.getCanonicalName())
                        || (StringUtils.hasText(c.getDisplayName()) && text.contains(c.getDisplayName())))
                .toList();
    }

    private String sentenceContaining(String content, String keyword) {
        if (!StringUtils.hasText(content) || !StringUtils.hasText(keyword)) {
            return null;
        }
        int idx = content.indexOf(keyword);
        if (idx < 0) {
            return truncate(content, SENTENCE_MAX_LENGTH);
        }
        int start = idx;
        while (start > 0 && !isSentenceBoundary(content.charAt(start - 1))) {
            start--;
        }
        int end = idx + keyword.length();
        while (end < content.length() && !isSentenceBoundary(content.charAt(end))) {
            end++;
        }
        return truncate(content.substring(start, end).trim(), SENTENCE_MAX_LENGTH);
    }

    private boolean isSentenceBoundary(char c) {
        return c == '。' || c == '！' || c == '？' || c == '\n' || c == ';' || c == '；'
                || c == '.' || c == '!' || c == '?';
    }

    private String conceptName(Concept concept) {
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }

    /** 优先取前者，为空时退回后者。用于「用户自己的话到底存在哪个字段」这类二选一。 */
    private static String firstNonBlank(String preferred, String fallback) {
        return StringUtils.hasText(preferred) ? preferred : fallback;
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        String trimmed = text.trim();
        return trimmed.length() <= max ? trimmed : trimmed.substring(0, max) + "…";
    }
}
