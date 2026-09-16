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
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;
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
    /** 只为 {@link #markExpired} 的独立事务服务，见那里的说明 */
    private final PlatformTransactionManager transactionManager;

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

    /**
     * 待作答的测试。
     * <p>
     * 「已过期」在这里就会被挡掉，不等它被扫成 EXPIRED：状态位的翻转依赖用户下次提交
     * （见 {@link #markExpired}），中间这段时间如果照常列出来，用户点进去只会看到一个 409，
     * 属于「列表说有、操作说没有」。所以列表按<div>时间</div>判断，状态位只负责长期一致。
     */
    @Transactional(readOnly = true)
    public List<TestTaskResponse> listPending(Long userId) {
        LocalDateTime now = LocalDateTime.now();
        List<TestTask> tasks = testTaskMapper.selectList(new LambdaQueryWrapper<TestTask>()
                .eq(TestTask::getUserId, userId)
                .eq(TestTask::getStatus, TestTaskStatus.PENDING)
                // expired_at 为空的按「永不过期」处理（当前 create 一定会写，这里只是不制造额外假设）
                .and(w -> w.isNull(TestTask::getExpiredAt).or().gt(TestTask::getExpiredAt, now))
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
     * <p>
     * <b>两处顺序都是刻意的，别调换：</b>
     * <ol>
     *   <li><b>过期标记先写、且写在拿锁之前</b>。标记必须独立提交 —— 它后面紧跟着就 throw
     *       {@code BusinessException}（RuntimeException），写在同一事务里会被一起回滚，
     *       任务就永远停在 PENDING 继续出现在 listPending 里（见 {@link #markExpired}）。
     *       放在拿锁之前是因为标记跑在另一个事务里，此刻外层事务只做过普通 SELECT、没持有
     *       任何行锁，不会自己等自己。</li>
     *   <li><b>拿到行锁之后才判 PENDING</b>。「读状态」与「写 ANSWERED」之间不能有窗口，
     *       否则用户双击提交时两个请求同时读到 PENDING、同时插 test_answer，
     *       后一个撞 {@code uk_answer_task} 变成 500 —— 而接口契约写的是 409。</li>
     * </ol>
     */
    @Transactional
    public AnswerResult answer(Long userId, Long testId, AnswerRequest request) {
        TestTask snapshot = ownedTask(userId, testId);
        if (isExpired(snapshot)) {
            markExpired(testId);
            throw BusinessException.conflict("该测试已过期");
        }

        // SELECT ... FOR UPDATE：从这里到事务结束，同一 task 的其它提交请求全部排队
        TestTask task = testTaskMapper.selectByIdForUpdate(testId);
        if (task == null || !Objects.equals(task.getUserId(), userId)) {
            throw BusinessException.notFound("测试不存在");
        }
        if (task.getStatus() != TestTaskStatus.PENDING) {
            // 并发双击的第二个请求落在这里 —— 409，不是唯一键冲突的 500
            throw BusinessException.conflict("该测试已作答或已过期");
        }
        if (isExpired(task)) {
            // 极窄的窗口：上面那次检查还没到期，拿到锁时刚好到期。这里不再补标记
            // （本事务马上要回滚），交给下一次提交走 markExpired；listPending 也已经
            // 把过期的挡在外面，所以用户看不到这条已经作废的任务。
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

    private boolean isExpired(TestTask task) {
        return task.getExpiredAt() != null && task.getExpiredAt().isBefore(LocalDateTime.now());
    }

    /**
     * 把任务标成 EXPIRED，并<b>立刻独立提交</b>。
     * <p>
     * 为什么不能直接在外层事务里 {@code updateById}：调用方写完标记紧接着就 throw
     * {@code BusinessException}（RuntimeException），而 {@code @Transactional} 默认对
     * RuntimeException 回滚 —— 标记和异常一起被抹掉，任务永远停在 PENDING，
     * 既继续出现在 listPending 里，也永远不会被真正作废。
     * 之前就是栽在这里：写入写了，但没人看得到结果。
     * <p>
     * {@code PROPAGATION_REQUIRES_NEW} 把外层事务挂起，用一条新事务写完即提交，
     * 之后外层怎么回滚都与它无关。安全的前提是调用点<b>尚未持有 test_task 的行锁</b>
     * （答案提交里它排在 {@code selectByIdForUpdate} 之前），否则新事务会被外层自己挡住。
     * <p>
     * WHERE 带上 {@code status = PENDING}：只把「还待作答」的标成过期，
     * 不会覆盖 ANSWERED —— 否则一个已经答完的测试会被这里改写成 EXPIRED。
     */
    private void markExpired(Long testId) {
        TransactionTemplate template = new TransactionTemplate(transactionManager);
        template.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        template.executeWithoutResult(status -> testTaskMapper.update(null,
                new LambdaUpdateWrapper<TestTask>()
                        .eq(TestTask::getId, testId)
                        .eq(TestTask::getStatus, TestTaskStatus.PENDING)
                        .set(TestTask::getStatus, TestTaskStatus.EXPIRED)));
    }

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
