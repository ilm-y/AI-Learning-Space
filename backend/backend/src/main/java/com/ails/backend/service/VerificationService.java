package com.ails.backend.service;

import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningSpace;
import com.ails.backend.entity.TestTask;
import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.TestTaskStatus;
import com.ails.backend.enums.VerificationTrigger;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningSpaceMapper;
import com.ails.backend.mapper.TestTaskMapper;
import com.ails.backend.utils.JsonUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/**
 * 验证决策的落地：把 Agent 的 {@code ASK_NOW} 变成一条待作答的 TestTask，
 * 并在对话里插一条 {@code message_type = TEST} 的消息，让前端能在聊天流中渲染题卡。
 * <p>
 * <b>为什么不直接调用 {@link TestService#create}</b>：
 * {@code TestService} 依赖 {@link LearningAnalysisService}（作答后要跑 Evidence 链路），
 * 而本次的调用方 {@link AgentAnalysisWriteService} 又处在分析链路上，直接依赖会形成
 * {@code LearningAnalysisService → AgentAnalysisWriteService → TestService → LearningAnalysisService}
 * 的环。拆出本类后，依赖方向是单向的：验证服务只负责「建任务 + 通知前端」，
 * 不参与作答和分析。
 * <p>
 * <b>ASK_LATER 不在这里落库。</b> 目前没有调度器，也没有「待验证」表；这类决策由
 * Evidence 的 {@code need_follow_up} 承载（Agent 的候选证据自带该标记），
 * 将来做提醒时会从那里读，而不是在这里先写一条永远没人处理的任务。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class VerificationService {

    /** 与 {@link TestService} 保持一致：3 天内不作答即过期。 */
    private static final int DEFAULT_EXPIRE_DAYS = 3;

    private final TestTaskMapper testTaskMapper;
    private final ConceptMapper conceptMapper;
    private final LearningSpaceMapper spaceMapper;
    private final MessageService messageService;

    /**
     * 一次验证决策。字段与 Agent 的 {@code verificationDecision} 一一对应。
     *
     * @param trigger 为什么现在问 —— 只用于日志与排查，test_task 表不存它
     */
    public record VerificationDraft(
            Long userId,
            Long conversationId,
            Long conceptId,
            QuestionType questionType,
            String questionText,
            String correctAnswer,
            VerificationTrigger trigger,
            String reason) {
    }

    /**
     * 创建一条来自学习分析的任务，并往对话里插一条 TEST 消息。
     *
     * @return 新建的任务；不满足创建条件时返回 {@code null}（这不是错误，调用方照常继续）
     */
    @Transactional
    public TestTask createFromAnalysis(VerificationDraft draft) {
        if (draft == null || draft.userId() == null || draft.conceptId() == null) {
            return null;
        }
        // 归属校验不抛异常：Agent 的输出来自模型，一个越权的 conceptId 应该被丢掉，
        // 而不是让整轮分析的写入（证据 / 理解 / 记忆）跟着回滚。
        Concept concept = ownedConcept(draft.userId(), draft.conceptId());
        if (concept == null) {
            log.warn("跳过验证任务：知识点不存在或不属于该用户, userId={}, conceptId={}",
                    draft.userId(), draft.conceptId());
            return null;
        }
        if (hasPendingTask(draft.userId(), draft.conceptId())) {
            // 同一知识点已经有一道待作答的题。再插一条只会让用户在聊天里连着看到两张题卡，
            // 而且两张题最终会各自变成一条 TEST_RESULT 证据，重复惩罚同一个知识点。
            log.debug("跳过验证任务：该知识点已有待作答任务, userId={}, conceptId={}",
                    draft.userId(), draft.conceptId());
            return null;
        }

        QuestionType questionType = draft.questionType() == null
                ? QuestionType.SHORT_ANSWER : draft.questionType();
        String conceptName = displayName(concept);
        TestTask task = new TestTask();
        task.setUserId(draft.userId());
        task.setConceptId(concept.getId());
        task.setConversationId(draft.conversationId());
        task.setQuestionType(questionType);
        task.setQuestionText(StringUtils.hasText(draft.questionText())
                ? draft.questionText().trim()
                : defaultQuestion(questionType, conceptName));
        task.setCorrectAnswer(StringUtils.hasText(draft.correctAnswer())
                ? draft.correctAnswer().trim() : null);
        task.setStatus(TestTaskStatus.PENDING);
        task.setExpiredAt(LocalDateTime.now().plusDays(DEFAULT_EXPIRE_DAYS));
        testTaskMapper.insert(task);

        log.info("创建验证任务, userId={}, conceptId={}, testId={}, trigger={}, reason={}",
                draft.userId(), draft.conceptId(), task.getId(), draft.trigger(), draft.reason());
        appendTestMessage(task, conceptName);
        return task;
    }

    /**
     * 在对话流里插入题卡消息。
     * <p>
     * content 用 JSON 而不是自然语言：TEST 消息不是给模型读的文本，而是给前端渲染的结构化数据。
     * 拼成一句「请回答：……」会让人误以为它和普通助手消息一样能进历史，
     * 所以 {@link MessageService#history} 只取 TEXT 类型。
     */
    private void appendTestMessage(TestTask task, String conceptName) {
        if (task.getConversationId() == null) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("testId", task.getId());
        payload.put("conceptId", task.getConceptId());
        payload.put("conceptName", conceptName);
        payload.put("questionType", task.getQuestionType());
        payload.put("questionText", task.getQuestionText());
        payload.put("expiredAt", task.getExpiredAt());
        messageService.appendTestEvent(task.getConversationId(), JsonUtil.toJson(payload));
    }

    private boolean hasPendingTask(Long userId, Long conceptId) {
        Long pending = testTaskMapper.selectCount(new LambdaQueryWrapper<TestTask>()
                .eq(TestTask::getUserId, userId)
                .eq(TestTask::getConceptId, conceptId)
                .eq(TestTask::getStatus, TestTaskStatus.PENDING)
                // 已过期但还没被扫成 EXPIRED 的任务不算「待作答」，与 listPending 的口径一致
                .and(w -> w.isNull(TestTask::getExpiredAt)
                        .or().gt(TestTask::getExpiredAt, LocalDateTime.now())));
        return pending != null && pending > 0;
    }

    private Concept ownedConcept(Long userId, Long conceptId) {
        Concept concept = conceptMapper.selectById(conceptId);
        if (concept == null) {
            return null;
        }
        LearningSpace space = spaceMapper.selectById(concept.getLearningSpaceId());
        return space != null && Objects.equals(space.getUserId(), userId) ? concept : null;
    }

    /** 与 {@link TestService} 的默认题目措辞保持一致，避免同一个知识点出现两种提问风格。 */
    private static String defaultQuestion(QuestionType questionType, String conceptName) {
        return switch (questionType) {
            case SHORT_ANSWER -> "请用自己的话解释一下「" + conceptName + "」。";
            case TRUE_FALSE -> "「" + conceptName + "」我理解得对吗？请判断并说明理由。";
            case SINGLE_CHOICE -> "关于「" + conceptName + "」，下面哪种说法是对的？";
        };
    }

    private static String displayName(Concept concept) {
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }
}
