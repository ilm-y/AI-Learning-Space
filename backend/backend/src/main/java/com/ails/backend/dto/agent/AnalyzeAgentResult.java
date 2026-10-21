package com.ails.backend.dto.agent;

import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.VerificationAction;
import com.ails.backend.enums.VerificationTrigger;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.util.List;

/**
 * Agent 返回的一次学习分析结果。
 * <p>
 * <b>它只是候选集合，不是已完成的写入。</b> 后端仍要逐项校验：
 * conceptId 是否属于当前学习空间（Agent 侧已按「本轮检测到的概念」过滤，但那是
 * 另一层进程的保证，后端不能直接采信）、Evidence 是否已存在（dedupe_key）、
 * Memory 的作用域与生命周期、Test 是否值得插入。
 * <p>
 * 所有值类型都允许为 null：Agent 侧可能省略字段，而「省略」应当被当成
 * 「这一项没有建议」，不能让整个结果反序列化失败。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AnalyzeAgentResult(
        List<EvidenceCandidate> evidenceCandidates,
        UnderstandingProposal understandingProposal,
        List<MemoryCandidate> memoryCandidates,
        VerificationDecision verificationDecision,
        String analyzerVersion,
        List<String> detectedConcepts) {

    public AnalyzeAgentResult {
        evidenceCandidates = evidenceCandidates == null ? List.of() : List.copyOf(evidenceCandidates);
        memoryCandidates = memoryCandidates == null ? List.of() : List.copyOf(memoryCandidates);
        detectedConcepts = detectedConcepts == null ? List.of() : List.copyOf(detectedConcepts);
        verificationDecision = verificationDecision == null
                ? VerificationDecision.skip() : verificationDecision;
        analyzerVersion = analyzerVersion == null || analyzerVersion.isBlank()
                ? "agent-v1" : analyzerVersion;
    }

    /**
     * 一条候选证据。
     *
     * @param observation     用户原话或紧贴原话的短句，不是模型的结论
     * @param userExplanation 只有 USER_EXPLANATION / USER_CORRECTION 才应带值
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record EvidenceCandidate(
            Long conceptId,
            EvidenceType evidenceType,
            String observation,
            EvidenceCorrectness correctness,
            Float reasoningQuality,
            Float specificity,
            Float aiConfidence,
            Boolean needFollowUp,
            String userExplanation) {
    }

    /**
     * 对「当前理解」的增量提案。
     * <p>
     * 不是整份快照：后端只把这些条目合并进现有快照，误区仍走
     * {@code MisconceptionLifecycle} 的 ACTIVE / PENDING 生命周期。
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record UnderstandingProposal(
            Long conceptId,
            List<String> knownParts,
            List<String> uncertainParts,
            List<String> misconceptions,
            String latestUserExplanation) {

        public UnderstandingProposal {
            knownParts = knownParts == null ? List.of() : List.copyOf(knownParts);
            uncertainParts = uncertainParts == null ? List.of() : List.copyOf(uncertainParts);
            misconceptions = misconceptions == null ? List.of() : List.copyOf(misconceptions);
        }
    }

    /**
     * 一条 Memory 候选。Agent 推断的内容只能落 CANDIDATE。
     *
     * @param action 建议动作；后端只把它当作提示，最终仍按现有记忆的状态决定是否写入
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record MemoryCandidate(
            MemoryScopeType scopeType,
            Long learningSpaceId,
            Long conceptId,
            MemoryType memoryType,
            String memoryKey,
            String content,
            Float importance,
            MemoryReconcileAction action) {
    }

    /**
     * 验证决策。
     * <p>
     * {@code action = ASK_NOW} 且没有 {@code questionText} 时后端不会插题 ——
     * 「打断用户但什么都不问」比不打断更糟。
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record VerificationDecision(
            VerificationAction action,
            VerificationTrigger trigger,
            Long conceptId,
            String reason,
            QuestionType questionType,
            String questionText,
            String correctAnswer) {

        public VerificationDecision {
            action = action == null ? VerificationAction.SKIP : action;
        }

        public static VerificationDecision skip() {
            return new VerificationDecision(VerificationAction.SKIP, null, null, null, null, null, null);
        }
    }
}
