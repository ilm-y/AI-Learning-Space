package com.ails.backend.service;

import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.UnderstandingRevision;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.model.UnderstandingProposalDraft;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * {@link AgentAnalysisWriteService} 的理解写入分支。
 * <p>
 * 这里钉住的是「Agent 只给证据、或只给一部分提案时，用户表达与误区生命周期会不会丢」：
 * 提案只覆盖一个概念，而本轮证据往往涉及多个；早期实现在这种情况下既丢了
 * {@code userExplanation}，也从不推进误区（INCORRECT 建不出来、CORRECT 解除不掉）。
 */
class AgentAnalysisWriteServiceTest {

    private static final long USER_ID = 1L;
    private static final long SPACE_ID = 3L;
    private static final long CONCEPT_ID = 10L;

    private EvidenceService evidenceService;
    private UnderstandingService understandingService;
    private LearningStateService learningStateService;
    private AgentAnalysisWriteService service;

    private LearningUnderstanding understanding;

    @BeforeEach
    void setUp() {
        evidenceService = mock(EvidenceService.class);
        understandingService = mock(UnderstandingService.class);
        learningStateService = mock(LearningStateService.class);
        ConceptMapper conceptMapper = mock(ConceptMapper.class);

        Concept concept = new Concept();
        concept.setId(CONCEPT_ID);
        concept.setLearningSpaceId(SPACE_ID);
        when(conceptMapper.selectList(any())).thenReturn(List.of(concept));

        // 写入服务只依赖这六个协作者；Memory / Verification 在这几个用例里不参与
        service = new AgentAnalysisWriteService(
                evidenceService, understandingService, learningStateService,
                mock(MemoryService.class), mock(VerificationService.class), conceptMapper);

        understanding = new LearningUnderstanding();
        understanding.setId(500L);
        understanding.setConceptId(CONCEPT_ID);
        when(understandingService.getOrCreate(USER_ID, CONCEPT_ID)).thenReturn(understanding);
        when(understandingService.applyProposal(anyLong(), anyLong(), any(), any(), any()))
                .thenReturn(new UnderstandingService.ApplyResult(understanding, new UnderstandingRevision()));
        when(understandingService.markExposed(anyLong(), anyLong(), any()))
                .thenReturn(new UnderstandingService.ApplyResult(understanding, null));
    }

    @Test
    @DisplayName("有证据、无提案：userExplanation 仍写进理解快照")
    void keepsUserExplanationWhenProposalIsAbsent() {
        LearningEvidence evidence = newEvidence(900L);
        stubAppend(evidence);
        AnalyzeAgentResult result = result(
                candidate(EvidenceType.USER_EXPLANATION, "TLB 就是页表项的缓存"),
                null);

        service.apply(context(), result);

        ArgumentCaptor<UnderstandingProposalDraft> draft =
                ArgumentCaptor.forClass(UnderstandingProposalDraft.class);
        verify(understandingService).applyProposal(
                eq(USER_ID), eq(CONCEPT_ID), draft.capture(), eq(List.of(evidence)), eq(900L));
        assertThat(draft.getValue().latestUserExplanation()).isEqualTo("TLB 就是页表项的缓存");
        // 原话已经通过提案路径写入，就不能再走一次「纯误区演进」，否则同一轮会生成两条 Revision
        verify(understandingService, never()).evolveMisconceptions(anyLong(), anyLong(), any(), any());
    }

    @Test
    @DisplayName("提案覆盖的概念不再重复演进误区，且证据一并传下去")
    void proposalReceivesEvidenceAndSkipsEvolve() {
        LearningEvidence evidence = newEvidence(901L);
        stubAppend(evidence);
        AnalyzeAgentResult result = result(
                candidate(EvidenceType.USER_EXPLANATION, null),
                new AnalyzeAgentResult.UnderstandingProposal(
                        CONCEPT_ID, List.of("TLB 是页表项缓存"), List.of(), List.of(), null));

        service.apply(context(), result);

        verify(understandingService).applyProposal(
                eq(USER_ID), eq(CONCEPT_ID), any(), eq(List.of(evidence)), eq(901L));
        verify(understandingService, never()).evolveMisconceptions(anyLong(), anyLong(), any(), any());
    }

    @Test
    @DisplayName("既无提案也无原话：仍按本轮证据推进误区生命周期")
    void evolvesMisconceptionsWhenAgentOnlyReportsEvidence() {
        LearningEvidence first = newEvidence(902L);
        LearningEvidence second = newEvidence(903L);
        when(evidenceService.append(any()))
                .thenReturn(new EvidenceService.AppendResult(first, true))
                .thenReturn(new EvidenceService.AppendResult(second, true));
        AnalyzeAgentResult result = result(
                candidate(EvidenceType.OBSERVATION, null),
                candidate(EvidenceType.OBSERVATION, null),
                null);

        service.apply(context(), result);

        // 同一概念的两条证据要一起参与：一条 INCORRECT 建误区、一条足够强的 CORRECT 解除，
        // 只看最后一条会让结果依赖候选顺序。
        verify(understandingService).evolveMisconceptions(
                eq(USER_ID), eq(CONCEPT_ID), eq(List.of(first, second)), eq(903L));
        verify(understandingService, never()).applyProposal(anyLong(), anyLong(), any(), any(), any());
    }

    // ================= 构造工具 =================

    private static AgentAnalysisWriteService.TurnContext context() {
        return new AgentAnalysisWriteService.TurnContext(USER_ID, 100L, SPACE_ID, 1001L, 42L);
    }

    private static AnalyzeAgentResult result(
            AnalyzeAgentResult.EvidenceCandidate first,
            AnalyzeAgentResult.UnderstandingProposal proposal) {
        return result(first, null, proposal);
    }

    private static AnalyzeAgentResult result(
            AnalyzeAgentResult.EvidenceCandidate first,
            AnalyzeAgentResult.EvidenceCandidate second,
            AnalyzeAgentResult.UnderstandingProposal proposal) {
        List<AnalyzeAgentResult.EvidenceCandidate> candidates =
                second == null ? List.of(first) : List.of(first, second);
        return new AnalyzeAgentResult(candidates, proposal, List.of(), null, "agent-v1", List.of("TLB"));
    }

    private static AnalyzeAgentResult.EvidenceCandidate candidate(EvidenceType type, String explanation) {
        return new AnalyzeAgentResult.EvidenceCandidate(
                CONCEPT_ID, type, "TLB 的观察", EvidenceCorrectness.CORRECT,
                null, null, null, false, explanation);
    }

    private static LearningEvidence newEvidence(long id) {
        LearningEvidence evidence = new LearningEvidence();
        evidence.setId(id);
        evidence.setConceptId(CONCEPT_ID);
        return evidence;
    }

    private void stubAppend(LearningEvidence evidence) {
        when(evidenceService.append(any())).thenReturn(new EvidenceService.AppendResult(evidence, true));
    }
}
