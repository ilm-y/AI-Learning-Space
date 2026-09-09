package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.verification.AnswerRequest;
import com.ails.backend.dto.verification.AnswerResult;
import com.ails.backend.dto.verification.VerificationTaskResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningState;
import com.ails.backend.entity.VerificationAnswer;
import com.ails.backend.entity.VerificationTask;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.VerificationTaskStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import com.ails.backend.mapper.VerificationAnswerMapper;
import com.ails.backend.mapper.VerificationTaskMapper;
import com.ails.backend.utils.JsonUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 主动验证闭环（M1 本地桩）。
 * TODO(M5): 出题 / 判分 / 高权重证据应改由 Python 侧 Agent（Supervisor + 策略引擎）驱动。
 */
@Service
@RequiredArgsConstructor
public class VerificationService {

    private static final double VERIFICATION_EVIDENCE_WEIGHT = 1.2;

    private final VerificationTaskMapper taskMapper;
    private final VerificationAnswerMapper answerMapper;
    private final LearningEvidenceMapper evidenceMapper;
    private final ConceptMapper conceptMapper;
    private final LearningStateService learningStateService;

    @Transactional
    public List<VerificationTaskResponse> listPending(Long userId) {
        markExpired(userId);
        List<VerificationTask> tasks = taskMapper.selectList(new LambdaQueryWrapper<VerificationTask>()
                .eq(VerificationTask::getUserId, userId)
                .eq(VerificationTask::getStatus, VerificationTaskStatus.PENDING)
                .orderByAsc(VerificationTask::getCreatedAt));
        Map<Long, Concept> concepts = loadConcepts(
                tasks.stream().map(VerificationTask::getConceptId).toList());
        return tasks.stream().map(task -> toResponse(task, concepts)).toList();
    }

    @Transactional
    public AnswerResult answer(Long userId, Long taskId, AnswerRequest request) {
        VerificationTask task = taskMapper.selectById(taskId);
        if (task == null || !task.getUserId().equals(userId)) {
            throw BusinessException.notFound("验证题不存在");
        }
        if (task.getStatus() != VerificationTaskStatus.PENDING) {
            throw BusinessException.conflict("该验证题已作答或已失效");
        }

        boolean correct = grade(task, request.userAnswer());

        VerificationAnswer answer = new VerificationAnswer();
        answer.setTaskId(taskId);
        answer.setUserAnswer(request.userAnswer());
        answer.setIsCorrect(correct);
        answerMapper.insert(answer);

        task.setStatus(VerificationTaskStatus.ANSWERED);
        taskMapper.updateById(task);

        // —— 高权重证据：主动验证回答 weight=1.2 ——
        LearningEvidence evidence = new LearningEvidence();
        evidence.setUserId(userId);
        evidence.setConceptId(task.getConceptId());
        evidence.setSourceType(EvidenceSourceType.VERIFICATION);
        evidence.setSourceId(answer.getId());
        evidence.setObservation(String.format(
                "主动验证【%s】%s：用户回答「%s」，判定%s",
                task.getQuestionType(), task.getQuestionText(), request.userAnswer(),
                correct ? "正确" : "错误"));
        evidence.setCorrectness(correct ? EvidenceCorrectness.CORRECT : EvidenceCorrectness.INCORRECT);
        evidence.setWeight(VERIFICATION_EVIDENCE_WEIGHT);
        evidence.setAiConfidence(0.95);
        evidenceMapper.insert(evidence);

        LearningState state = learningStateService.recompute(userId, task.getConceptId());
        String conceptName = conceptName(conceptMapper.selectById(task.getConceptId()));
        String feedback;
        if (correct) {
            feedback = String.format("回答正确！“%s”掌握度已更新，下次复习安排在三天后。", conceptName);
        } else {
            feedback = String.format("回答错误，已记录误解并安排明天复习“%s”。", conceptName);
        }
        return new AnswerResult(correct, feedback, state.getMastery(), state.getConfidence());
    }

    /**
     * 创建验证题（为后续 Python 出题 / 本地演示预留）。要求知识点属于该用户的空间。
     */
    @Transactional
    public VerificationTask createVerificationTask(Long userId, Long conceptId, QuestionType type,
                                                   String questionText, List<String> options,
                                                   String correctAnswer, String hint, LocalDateTime expiredAt) {
        Concept concept = conceptMapper.selectById(conceptId);
        if (concept == null) {
            throw BusinessException.notFound("知识点不存在");
        }
        VerificationTask task = new VerificationTask();
        task.setUserId(userId);
        task.setConceptId(conceptId);
        task.setQuestionType(type);
        task.setQuestionText(questionText);
        task.setOptions(options == null || options.isEmpty() ? null : JsonUtil.fromStringList(options));
        task.setCorrectAnswer(correctAnswer);
        task.setHint(hint);
        task.setStatus(VerificationTaskStatus.PENDING);
        task.setExpiredAt(expiredAt);
        taskMapper.insert(task);
        return task;
    }

    // ================= 内部工具 =================

    /** 到期未答的任务标记为 EXPIRED */
    private void markExpired(Long userId) {
        List<VerificationTask> expired = taskMapper.selectList(
                new LambdaQueryWrapper<VerificationTask>()
                        .eq(VerificationTask::getUserId, userId)
                        .eq(VerificationTask::getStatus, VerificationTaskStatus.PENDING)
                        .isNotNull(VerificationTask::getExpiredAt)
                        .lt(VerificationTask::getExpiredAt, LocalDateTime.now()));
        for (VerificationTask task : expired) {
            task.setStatus(VerificationTaskStatus.EXPIRED);
            taskMapper.updateById(task);
        }
    }

    /**
     * 本地桩式判题。TODO(M5): 由 Python Agent 判分（尤其 SHORT_ANSWER 应语义判断）。
     */
    private boolean grade(VerificationTask task, String userAnswer) {
        String correct = task.getCorrectAnswer();
        switch (task.getQuestionType()) {
            case TRUE_FALSE -> {
                Boolean given = parseBoolean(userAnswer);
                Boolean expected = parseBoolean(correct);
                return given != null && expected != null && given.equals(expected);
            }
            case MULTIPLE_CHOICE -> {
                Character given = choiceLetter(userAnswer);
                Character expected = choiceLetter(correct);
                return given != null && expected != null && given.equals(expected);
            }
            case FILL_BLANK, SHORT_ANSWER -> {
                // 用 | 分隔可接受的多个答案
                String given = normalize(userAnswer);
                for (String accept : correct.split("\\|")) {
                    if (given.equals(normalize(accept))) {
                        return true;
                    }
                }
                return false;
            }
            default -> throw new IllegalStateException("未知题型: " + task.getQuestionType());
        }
    }

    private String normalize(String s) {
        if (s == null) {
            return "";
        }
        return s.trim().toLowerCase().replaceAll("[\\s\\p{Punct}。，、]+", "");
    }

    private Boolean parseBoolean(String s) {
        if (s == null) {
            return null;
        }
        String v = s.trim().toLowerCase();
        if (v.matches("true|t|yes|y|√|对|正确|是")) {
            return Boolean.TRUE;
        }
        if (v.matches("false|f|no|n|×|x|错|错误|否")) {
            return Boolean.FALSE;
        }
        return null;
    }

    /** 提取第一个 A~D 字母作为选项 */
    private Character choiceLetter(String s) {
        if (s == null) {
            return null;
        }
        for (char c : s.trim().toUpperCase().toCharArray()) {
            if (c >= 'A' && c <= 'D') {
                return c;
            }
        }
        return null;
    }

    private VerificationTaskResponse toResponse(VerificationTask task, Map<Long, Concept> concepts) {
        return new VerificationTaskResponse(
                task.getId(), task.getConceptId(), conceptName(concepts.get(task.getConceptId())),
                task.getQuestionType(), task.getQuestionText(), JsonUtil.toStringList(task.getOptions()),
                task.getHint(), task.getStatus(), task.getExpiredAt(), task.getCreatedAt());
    }

    private Map<Long, Concept> loadConcepts(List<Long> ids) {
        List<Long> distinct = ids.stream().filter(java.util.Objects::nonNull).distinct().toList();
        if (distinct.isEmpty()) {
            return Collections.emptyMap();
        }
        return conceptMapper.selectBatchIds(distinct).stream()
                .collect(Collectors.toMap(Concept::getId, Function.identity()));
    }

    private String conceptName(Concept concept) {
        if (concept == null) {
            return null;
        }
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName() : concept.getCanonicalName();
    }
}
