package com.ails.backend.service;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Highlight;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.Message;
import com.ails.backend.entity.TestAnswer;
import com.ails.backend.entity.TestTask;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
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

    /** 本地规则分析器版本，参与 Evidence 去重 */
    public static final String ANALYZER_VERSION = "rules-v1";

    /** 观察文本入库前的截断长度 */
    private static final int OBSERVATION_MAX_LENGTH = 200;
    /** 一句话的截断长度，用于从消息中摘出提及该概念的句子 */
    private static final int SENTENCE_MAX_LENGTH = 160;

    private final EvidenceService evidenceService;
    private final UnderstandingService understandingService;
    private final LearningStateService learningStateService;
    private final AgentRunMapper agentRunMapper;
    private final ConceptMapper conceptMapper;
    private final ConversationMapper conversationMapper;

    /**
     * 分析一条消息。可能抽出 0 / 1 / N 条证据（每个被提到的 Concept 各一条）。
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
        List<Concept> matched = matchConcepts(conversation.getLearningSpaceId(), message.getContent());
        if (matched.isEmpty()) {
            // 闲聊：没有任何已知 Concept 被提到，不产生证据
            return List.of();
        }
        boolean looksLikeQuestion = message.getContent().contains("?") || message.getContent().contains("？");

        List<Extracted> items = matched.stream()
                .map(concept -> new Extracted(
                        concept.getId(),
                        sentenceContaining(message.getContent(), conceptName(concept)),
                        // 单纯「提到」不等于理解，只说明接触过（EXPOSED）
                        EvidenceCorrectness.UNKNOWN,
                        null, null, null,
                        looksLikeQuestion,
                        null,
                        EvidenceSourceType.MESSAGE,
                        message.getId()))
                .toList();

        return runAnalysis(conversation.getUserId(), conversation.getId(), AgentRunType.EXTRACTION,
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

        String observation = truncate(StringUtils.hasText(highlight.getNote())
                ? highlight.getNote() : highlight.getSelectedText(), OBSERVATION_MAX_LENGTH);
        // 用户自己的解释是最强信号，单独留存为 latestUserExplanation
        String userExplanation = highlight.getMarkType() == HighlightType.MY_UNDERSTANDING
                ? observation : null;

        List<Extracted> items = new ArrayList<>();
        for (Concept concept : matched) {
            items.add(new Extracted(
                    concept.getId(), observation, correctness,
                    highlight.getMarkType() == HighlightType.MY_UNDERSTANDING ? 0.8f : null,
                    null, null,
                    highlight.getMarkType() == com.ails.backend.enums.HighlightType.QUESTION,
                    userExplanation,
                    EvidenceSourceType.HIGHLIGHT,
                    highlight.getId()));
        }

        return runAnalysis(highlight.getUserId(), conversation.getId(), AgentRunType.EXTRACTION,
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
                task.getConceptId(), observation,
                correctness == null ? EvidenceCorrectness.UNKNOWN : correctness,
                // 测试回答是结构化验证，推理质量与把握度都高于自由聊天
                0.9f, null, 0.9f,
                correctness != EvidenceCorrectness.CORRECT,
                answer.getUserAnswer(),
                EvidenceSourceType.TEST_ANSWER,
                answer.getId());

        return runAnalysis(task.getUserId(), task.getConversationId(), AgentRunType.TEST,
                "analyze testAnswer#" + answer.getId(), List.of(item));
    }

    // ================= 编排 =================

    private record Extracted(
            Long conceptId,
            String observation,
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
     * 按固定顺序跑完整条链路，整段由一条 agent_run 串起来（也是 Evidence 去重的依据）。
     */
    private List<LearningEvidence> runAnalysis(Long userId, Long conversationId, AgentRunType runType,
                                               String inputSummary, List<Extracted> items) {
        if (items.isEmpty()) {
            return List.of();
        }
        LocalDateTime startedAt = LocalDateTime.now();
        AgentRun run = new AgentRun();
        run.setUserId(userId);
        run.setConversationId(conversationId);
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

                // 2. 追加证据（append-only + 查重）
                LearningEvidence evidence = evidenceService.append(new EvidenceService.Draft(
                        userId,
                        item.conceptId(),
                        understanding.getId(),
                        item.observation(),
                        item.correctness(),
                        item.reasoningQuality(),
                        item.specificity(),
                        item.aiConfidence(),
                        item.needFollowUp(),
                        run.getId(),
                        ANALYZER_VERSION,
                        null,
                        List.of(new EvidenceService.SourceRef(item.sourceType(), item.sourceId()))));
                if (evidence == null) {
                    // 同一次 run 内重复写入，跳到下一个
                    continue;
                }
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
            run.setErrorStack(truncate(e.toString(), 1000));
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

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        String trimmed = text.trim();
        return trimmed.length() <= max ? trimmed : trimmed.substring(0, max) + "…";
    }
}
