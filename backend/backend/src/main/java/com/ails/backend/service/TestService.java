package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.test.AnswerRequest;
import com.ails.backend.dto.test.AnswerResult;
import com.ails.backend.dto.test.TestTaskRequest;
import com.ails.backend.dto.test.TestTaskResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.TestAnswer;
import com.ails.backend.entity.TestTask;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.TestTaskStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.TestAnswerMapper;
import com.ails.backend.mapper.TestTaskMapper;
import com.ails.backend.utils.JsonUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * 主动验证：出题 → 作答 → 判分 → 变成证据。
 * <p>
 * 测试不是「另开一个考试页面」，而是嵌在当前对话里的一次追问，所以 test_task 挂在
 * conversation 上。是否出题由 Evidence Gap 与验证策略决定，不是「分数低就出题」——
 * 这里只负责执行一次已经决定好的验证。
 * <p>
 * 判分后的结果会立刻交给 {@link LearningAnalysisService}，走完整的
 * Evidence → Understanding → Revision → LearningState 链路：
 * 测试回答是理解状态从 PARTIAL 走向 DEMONSTRATED（或暴露为 MISCONCEPTION）的主要途径。
 * <p>
 * 当前判分为本地规则版，接入 Python Agent 后由 Agent 的评价结果替换。
 */
@Service
@RequiredArgsConstructor
public class TestService {

    /** 测试任务默认有效期 */
    private static final int DEFAULT_EXPIRE_DAYS = 3;
    /** 简答题认为「答得像回事」的最短长度 */
    private static final int SHORT_ANSWER_MIN_LENGTH = 20;
    /** 简答题能形成部分证据的最短长度 */
    private static final int SHORT_ANSWER_PARTIAL_LENGTH = 8;

    private final TestTaskMapper testTaskMapper;
    private final TestAnswerMapper testAnswerMapper;
    private final ConceptMapper conceptMapper;
    private final ConceptService conceptService;
    private final ConversationService conversationService;
    private final UnderstandingService understandingService;
    private final LearningAnalysisService learningAnalysisService;

    @Transactional
    public TestTaskResponse create(Long userId, TestTaskRequest request) {
        Concept concept = conceptService.accessibleConcept(userId, request.conceptId());
        if (request.conversationId() != null) {
            conversationService.ownedConversation(userId, request.conversationId());
        }

        QuestionType questionType = request.questionType() == null
                ? QuestionType.SHORT_ANSWER : request.questionType();
        TestTask task = new TestTask();
        task.setUserId(userId);
        task.setConceptId(concept.getId());
        task.setConversationId(request.conversationId());
        task.setQuestionType(questionType);
        task.setQuestionText(StringUtils.hasText(request.questionText())
                ? request.questionText().trim()
                : defaultQuestion(questionType, displayName(concept)));
        task.setCorrectAnswer(request.correctAnswer());
        task.setEvaluationRubric(request.evaluationRubric());
        task.setStatus(TestTaskStatus.PENDING);
        task.setExpiredAt(LocalDateTime.now().plusDays(DEFAULT_EXPIRE_DAYS));
        testTaskMapper.insert(task);

        return TestTaskResponse.from(task, displayName(concept));
    }

    @Transactional(readOnly = true)
    public List<TestTaskResponse> listPending(Long userId) {
        List<TestTask> tasks = testTaskMapper.selectList(new LambdaQueryWrapper<TestTask>()
                .eq(TestTask::getUserId, userId)
                .eq(TestTask::getStatus, TestTaskStatus.PENDING)
                .orderByDesc(TestTask::getCreatedAt));
        return tasks.stream()
                .map(task -> TestTaskResponse.from(task, conceptName(task.getConceptId())))
                .toList();
    }

    @Transactional(readOnly = true)
    public TestTaskResponse get(Long userId, Long testId) {
        TestTask task = ownedTask(userId, testId);
        return TestTaskResponse.from(task, conceptName(task.getConceptId()));
    }

    /**
     * 提交答案：判分 → 落 test_answer → 推进理解链路。
     */
    @Transactional
    public AnswerResult answer(Long userId, Long testId, AnswerRequest request) {
        TestTask task = ownedTask(userId, testId);
        if (task.getStatus() != TestTaskStatus.PENDING) {
            throw BusinessException.conflict("该测试已作答或已过期");
        }
        if (task.getExpiredAt() != null && task.getExpiredAt().isBefore(LocalDateTime.now())) {
            task.setStatus(TestTaskStatus.EXPIRED);
            testTaskMapper.updateById(task);
            throw BusinessException.conflict("该测试已过期");
        }

        String userAnswer = request.userAnswer().trim();
        Evaluation evaluation = evaluate(task, userAnswer);

        TestAnswer answer = new TestAnswer();
        answer.setTaskId(task.getId());
        answer.setUserAnswer(userAnswer);
        answer.setIsCorrect(evaluation.correctness() == EvidenceCorrectness.CORRECT);
        answer.setEvaluation(JsonUtil.toJson(evaluation));
        testAnswerMapper.insert(answer);

        task.setStatus(TestTaskStatus.ANSWERED);
        testTaskMapper.updateById(task);

        List<LearningEvidence> evidences =
                learningAnalysisService.analyzeTestAnswer(answer, task, evaluation.correctness());
        LearningEvidence evidence = evidences.isEmpty() ? null : evidences.get(0);

        LearningUnderstanding understanding = understandingService.find(task.getUserId(), task.getConceptId());
        return new AnswerResult(
                answer.getIsCorrect(),
                evaluation.feedback(),
                evidence == null ? null : evidence.getId(),
                understanding == null ? null : understanding.getCurrentRevisionId(),
                understanding == null ? null : understanding.getStatus());
    }

    // ================= 判分 =================

    private record Evaluation(EvidenceCorrectness correctness, String feedback) {
    }

    private Evaluation evaluate(TestTask task, String userAnswer) {
        return switch (task.getQuestionType()) {
            case SINGLE_CHOICE, TRUE_FALSE -> evaluateExact(task, userAnswer);
            case SHORT_ANSWER -> evaluateShortAnswer(task, userAnswer);
        };
    }

    private Evaluation evaluateExact(TestTask task, String userAnswer) {
        boolean correct = StringUtils.hasText(task.getCorrectAnswer())
                && normalizeAnswer(task.getCorrectAnswer()).equals(normalizeAnswer(userAnswer));
        return new Evaluation(
                correct ? EvidenceCorrectness.CORRECT : EvidenceCorrectness.INCORRECT,
                correct ? "回答正确。" : "回答不正确，可以再想想。");
    }

    /**
     * 简答题判分（本地规则版）。
     * <p>
     * 有标准答案时按关键词覆盖率判：全部命中 → CORRECT，命中一部分 → PARTIAL，都没中 → INCORRECT。
     * 没有标准答案时退化为长度 + 是否点到概念的启发式 —— 这显然不是真正的语义判断，
     * 等 Agent 接管后整段替换。
     */
    private Evaluation evaluateShortAnswer(TestTask task, String userAnswer) {
        if (StringUtils.hasText(task.getCorrectAnswer())) {
            List<String> keywords = splitKeywords(task.getCorrectAnswer());
            long hit = keywords.stream().filter(userAnswer::contains).count();
            if (!keywords.isEmpty() && hit == keywords.size()) {
                return new Evaluation(EvidenceCorrectness.CORRECT, "回答覆盖了要点。");
            }
            if (hit > 0) {
                return new Evaluation(EvidenceCorrectness.PARTIAL,
                        "答对了一部分，还缺：" + keywords.stream().filter(k -> !userAnswer.contains(k)).toList());
            }
            return new Evaluation(EvidenceCorrectness.INCORRECT, "没有答到要点，可以回到对话里再看看。");
        }

        String conceptName = conceptName(task.getConceptId());
        boolean mentionsConcept = StringUtils.hasText(conceptName) && userAnswer.contains(conceptName);
        if (userAnswer.length() >= SHORT_ANSWER_MIN_LENGTH && mentionsConcept) {
            return new Evaluation(EvidenceCorrectness.CORRECT, "看得出你确实理解了，继续保持。");
        }
        if (userAnswer.length() >= SHORT_ANSWER_PARTIAL_LENGTH) {
            return new Evaluation(EvidenceCorrectness.PARTIAL, "方向对了，可以再展开一点。");
        }
        return new Evaluation(EvidenceCorrectness.INCORRECT, "回答太简短，试着用自己的话完整说一遍。");
    }

    private List<String> splitKeywords(String text) {
        List<String> keywords = new ArrayList<>();
        for (String part : text.split("[,，;；|/]")) {
            if (StringUtils.hasText(part)) {
                keywords.add(part.trim());
            }
        }
        return keywords;
    }

    private String normalizeAnswer(String text) {
        return text == null ? "" : text.trim().replaceAll("\\s+", "").toLowerCase();
    }

    private String defaultQuestion(QuestionType questionType, String conceptName) {
        return switch (questionType) {
            case SHORT_ANSWER -> "请用自己的话解释一下「" + conceptName + "」。";
            case TRUE_FALSE -> "「" + conceptName + "」我理解得对吗？请判断并说明理由。";
            case SINGLE_CHOICE -> "关于「" + conceptName + "」，下面哪种说法是对的？";
        };
    }

    // ================= 内部工具 =================

    private TestTask ownedTask(Long userId, Long testId) {
        TestTask task = testTaskMapper.selectById(testId);
        if (task == null || !Objects.equals(task.getUserId(), userId)) {
            throw BusinessException.notFound("测试不存在");
        }
        return task;
    }

    private String conceptName(Long conceptId) {
        Concept concept = conceptMapper.selectById(conceptId);
        return displayName(concept);
    }

    private String displayName(Concept concept) {
        if (concept == null) {
            return null;
        }
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }
}
