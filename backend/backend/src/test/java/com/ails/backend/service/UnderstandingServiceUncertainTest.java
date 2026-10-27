package com.ails.backend.service;

import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningUnderstanding;
import com.ails.backend.entity.UnderstandingRevision;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.UnderstandingStatus;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningUnderstandingMapper;
import com.ails.backend.mapper.UnderstandingRevisionMapper;
import com.ails.backend.model.UnderstandingProposalDraft;
import com.ails.backend.utils.JsonUtil;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * {@code uncertainParts} 的解除规则。
 * <p>
 * 这里钉住两件事：
 * <ol>
 *   <li>被后续正确证据<b>原样</b>确认的条目必须从 uncertainParts 移除，否则概念永远
 *       达不到 DEMONSTRATED —— 这曾经是一条只增不减的死路；</li>
 *   <li>解除必须按完全相同文本判断，不能因为一条正确证据就把所有不确定项清空。</li>
 * </ol>
 * 同时确认每次真实变化只产生一条 Revision。
 */
class UnderstandingServiceUncertainTest {

    private static final long USER_ID = 1L;
    private static final long CONCEPT_ID = 10L;
    private static final long UNDERSTANDING_ID = 500L;

    private LearningUnderstandingMapper understandingMapper;
    private UnderstandingRevisionMapper revisionMapper;
    private UnderstandingService service;

    @BeforeEach
    void setUp() {
        understandingMapper = mock(LearningUnderstandingMapper.class);
        revisionMapper = mock(UnderstandingRevisionMapper.class);
        service = new UnderstandingService(
                understandingMapper,
                revisionMapper,
                mock(ConceptMapper.class),
                mock(ConceptService.class));
        when(revisionMapper.selectMaxRevisionNoForUpdate(anyLong())).thenReturn(null);
        when(revisionMapper.insert(any(UnderstandingRevision.class))).thenAnswer(invocation -> {
            UnderstandingRevision revision = invocation.getArgument(0);
            revision.setId(900L);
            return 1;
        });
    }

    @Test
    @DisplayName("PARTIAL 同时进入 known 与 uncertain，CORRECT 原样确认后解除并允许 DEMONSTRATED")
    void correctEvidenceResolvesTheSameUncertainItem() {
        String first = "TLB 缓存的是页表项";
        String second = "TLB 命中时不需要访问页表";
        LearningUnderstanding snapshot = snapshot(
                UnderstandingStatus.PARTIAL, List.of(first, second), List.of(first));
        when(understandingMapper.selectByUserAndConceptForUpdate(USER_ID, CONCEPT_ID))
                .thenReturn(snapshot);

        UnderstandingService.ApplyResult result = service.apply(
                USER_ID, CONCEPT_ID,
                evidence(first, EvidenceCorrectness.CORRECT),
                null);

        // 同一文本被后续正确证据确认 → 解除，状态才能继续推进到 DEMONSTRATED
        assertThat(JsonUtil.toStringList(result.understanding().getUncertainParts()))
                .isEmpty();
        assertThat(JsonUtil.toStringList(result.understanding().getKnownParts()))
                .containsExactly(first, second);
        assertThat(result.understanding().getStatus()).isEqualTo(UnderstandingStatus.DEMONSTRATED);
        assertThat(result.revision()).isNotNull();
        assertThat(result.revision().getRevisionNo()).isEqualTo(1);
    }

    @Test
    @DisplayName("措辞不同但语义相近的条目不会因一条正确证据被清空")
    void correctEvidenceDoesNotClearDifferentWording() {
        String known = "TLB 缓存的是页表项";
        LearningUnderstanding snapshot = snapshot(
                UnderstandingStatus.PARTIAL, List.of(known), List.of("TLB 的表项何时失效？"));
        when(understandingMapper.selectByUserAndConceptForUpdate(USER_ID, CONCEPT_ID))
                .thenReturn(snapshot);

        UnderstandingService.ApplyResult result = service.apply(
                USER_ID, CONCEPT_ID,
                evidence(known, EvidenceCorrectness.CORRECT),
                null);

        assertThat(JsonUtil.toStringList(result.understanding().getUncertainParts()))
                .containsExactly("TLB 的表项何时失效？");
        assertThat(result.understanding().getStatus()).isEqualTo(UnderstandingStatus.PARTIAL);
    }

    @Test
    @DisplayName("提案里 known 与 uncertain 重复的同一表述只保留在 known 中")
    void proposalDropsUncertainDuplicatedByKnown() {
        LearningUnderstanding snapshot = snapshot(
                UnderstandingStatus.UNKNOWN, List.of(), List.of());
        when(understandingMapper.selectByUserAndConceptForUpdate(USER_ID, CONCEPT_ID))
                .thenReturn(snapshot);

        UnderstandingProposalDraft proposal = new UnderstandingProposalDraft(
                List.of("TLB 缓存的是页表项"),
                List.of("TLB 缓存的是页表项", "TLB 的替换策略是什么"),
                List.of(),
                null);

        UnderstandingService.ApplyResult result = service.applyProposal(
                USER_ID, CONCEPT_ID, proposal, List.of(), 77L);

        assertThat(JsonUtil.toStringList(result.understanding().getKnownParts()))
                .containsExactly("TLB 缓存的是页表项");
        assertThat(JsonUtil.toStringList(result.understanding().getUncertainParts()))
                .containsExactly("TLB 的替换策略是什么");
        assertThat(result.revision()).isNotNull();
        assertThat(result.revision().getTriggerEvidenceId()).isEqualTo(77L);
    }

    @Test
    @DisplayName("内容变化时刷新 updatedAt，并与新 Revision 时间保持一致")
    void contentChangeRefreshesUpdatedAtAndKeepsRevisionConsistent() {
        LocalDateTime old = LocalDateTime.of(2020, 1, 1, 0, 0);
        String known = "TLB 缓存的是页表项";
        LearningUnderstanding snapshot = snapshot(
                UnderstandingStatus.PARTIAL, List.of(known), List.of());
        snapshot.setSummary("已理解：" + known);
        snapshot.setUpdatedAt(old);
        when(understandingMapper.selectByUserAndConceptForUpdate(USER_ID, CONCEPT_ID))
                .thenReturn(snapshot);

        UnderstandingService.ApplyResult result = service.apply(
                USER_ID, CONCEPT_ID,
                evidence("TLB 命中时不需要访问页表", EvidenceCorrectness.CORRECT),
                null);

        assertThat(result.revision()).isNotNull();
        // 用一个远早于 now 的固定旧值断言「确实刷新了」，不依赖毫秒级执行时差。
        assertThat(result.understanding().getUpdatedAt()).isAfter(old);
        assertThat(result.revision().getCreatedAt())
                .isEqualTo(result.understanding().getUpdatedAt());
        assertThat(result.understanding().getCurrentRevisionId())
                .isEqualTo(result.revision().getId());
    }

    @Test
    @DisplayName("内容没有变化时不写 Revision，也不刷新 updatedAt")
    void noChangeDoesNotCreateRevision() {
        String known = "TLB 缓存的是页表项";
        LearningUnderstanding snapshot = snapshot(
                UnderstandingStatus.PARTIAL, List.of(known), List.of());
        // 已持久化的快照一定带着上次生成的 summary；不补上会让「summary 从 null
        // 变成非 null」被当成真实变化。这里要与 buildSummary 的口径一致。
        snapshot.setSummary("已理解：" + known);
        LocalDateTime unchangedSince = LocalDateTime.of(2020, 1, 1, 0, 0);
        snapshot.setUpdatedAt(unchangedSince);
        when(understandingMapper.selectByUserAndConceptForUpdate(USER_ID, CONCEPT_ID))
                .thenReturn(snapshot);

        UnderstandingService.ApplyResult result = service.apply(
                USER_ID, CONCEPT_ID,
                evidence(known, EvidenceCorrectness.CORRECT),
                null);

        assertThat(result.revision()).isNull();
        assertThat(result.understanding().getUpdatedAt()).isEqualTo(unchangedSince);
    }

    private static LearningUnderstanding snapshot(UnderstandingStatus status,
                                                  List<String> known,
                                                  List<String> uncertain) {
        LearningUnderstanding understanding = new LearningUnderstanding();
        understanding.setId(UNDERSTANDING_ID);
        understanding.setUserId(USER_ID);
        understanding.setConceptId(CONCEPT_ID);
        understanding.setStatus(status);
        understanding.setKnownParts(JsonUtil.fromStringList(known));
        understanding.setUncertainParts(JsonUtil.fromStringList(uncertain));
        understanding.setMisconceptions(
                com.ails.backend.model.MisconceptionLifecycle.serialize(List.of()));
        understanding.setSummary(null);
        return understanding;
    }

    private static LearningEvidence evidence(String observation,
                                             EvidenceCorrectness correctness) {
        LearningEvidence evidence = new LearningEvidence();
        evidence.setId(123L);
        evidence.setUserId(USER_ID);
        evidence.setConceptId(CONCEPT_ID);
        evidence.setObservation(observation);
        evidence.setCorrectness(correctness);
        return evidence;
    }
}
