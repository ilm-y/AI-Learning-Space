package com.ails.backend.service;

import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.service.EvidenceService.Draft;
import com.ails.backend.service.EvidenceService.SourceRef;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Evidence 去重指纹的规则测试。
 * <p>
 * {@code dedupeKey} 是包级纯函数，所以这里不起 Spring 上下文、不连 MySQL ——
 * 「什么算同一条证据」这件事可以单独回归。
 * <p>
 * 这个测试存在的理由是 B1 那次修改：旧口径是 {@code UNIQUE(agent_run_id, concept_id,
 * observation_hash)}，它把「同一条观察被两次分析看到」错判成两条证据 —— 而且跨 run 的
 * 重复<b>连唯一索引都拦不住</b>（{@code AUDIT_NOTES.md} §1 记的就是这条）。新口径把
 * {@code agent_run_id} 从指纹里摘掉，所以下面第一条用例才是这次修改的核心断言。
 */
class EvidenceDedupeKeyTest {

    private static Draft draft(Long runId, String observation) {
        return new Draft(
                1L, 23L, 500L,
                EvidenceType.OBSERVATION,
                observation,
                EvidenceCorrectness.UNKNOWN,
                null, null, null,
                false,
                runId,
                "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L)));
    }

    private static Draft highlightDraft(Long runId, String observation) {
        return new Draft(
                1L, 23L, 500L,
                EvidenceType.USER_EXPLANATION,
                observation,
                EvidenceCorrectness.CORRECT,
                0.8f, null, null,
                false,
                runId,
                "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.HIGHLIGHT, 7L)));
    }

    // ================= 指纹不含 agent_run_id =================

    @Test
    @DisplayName("同一条观察换个 run 再分析一次：指纹不变（这是 B1 的核心断言）")
    void runIdDoesNotAffectTheKey() {
        assertThat(EvidenceService.dedupeKey(draft(1L, "他在讨论 TLB")))
                .isEqualTo(EvidenceService.dedupeKey(draft(2L, "他在讨论 TLB")));
    }

    // ================= normalize 参与指纹 =================

    @Test
    @DisplayName("大小写与空白差异归一后算同一条：不会因为多敲一个空格就多出一条证据")
    void normalizationFoldsCaseAndWhitespace() {
        assertThat(EvidenceService.dedupeKey(draft(1L, "他在讨论 TLB")))
                .isEqualTo(EvidenceService.dedupeKey(draft(1L, "  他在讨论   tlb ")));
    }

    @Test
    @DisplayName("观察内容真的变了就是另一条证据 —— 归一化不掩盖语义差异")
    void differentObservationsAreDifferentKeys() {
        assertThat(EvidenceService.dedupeKey(draft(1L, "他在讨论 TLB")))
                .isNotEqualTo(EvidenceService.dedupeKey(draft(1L, "他在讨论页表")));
    }

    // ================= 各段都参与指纹 =================

    @Test
    @DisplayName("evidence_type 参与指纹：同一条观察，当观察记与当用户解释记是两条")
    void evidenceTypeSeparatesKeys() {
        assertThat(EvidenceService.dedupeKey(draft(1L, "TLB 是页表缓存")))
                .isNotEqualTo(EvidenceService.dedupeKey(highlightDraft(1L, "TLB 是页表缓存")));
    }

    @Test
    @DisplayName("主来源参与指纹：同一条文本来自不同 Message 是两条证据")
    void primarySourceSeparatesKeys() {
        Draft fromMessage88 = draft(1L, "TLB 是页表缓存");
        Draft fromMessage99 = new Draft(
                1L, 23L, 500L, EvidenceType.OBSERVATION, "TLB 是页表缓存",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 99L)));

        assertThat(EvidenceService.dedupeKey(fromMessage88))
                .isNotEqualTo(EvidenceService.dedupeKey(fromMessage99));
    }

    @Test
    @DisplayName("分隔符不是摆设：user=1,concept=23 与 user=12,concept=3 不能撞成同一条")
    void fieldsAreDelimitedSoConcatenationCollisionsCannotHappen() {
        Draft a = new Draft(1L, 23L, 500L, EvidenceType.OBSERVATION, "同一句话",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L)));
        Draft b = new Draft(12L, 3L, 500L, EvidenceType.OBSERVATION, "同一句话",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L)));

        assertThat(EvidenceService.dedupeKey(a)).isNotEqualTo(EvidenceService.dedupeKey(b));
    }

    @Test
    @DisplayName("只取第一个来源作主来源：后面挂的溯源来源不参与指纹")
    void onlyTheFirstSourceIsPrimary() {
        Draft oneSource = new Draft(1L, 23L, 500L, EvidenceType.OBSERVATION, "他在讨论 TLB",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L)));
        Draft twoSources = new Draft(1L, 23L, 500L, EvidenceType.OBSERVATION, "他在讨论 TLB",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L),
                        new SourceRef(EvidenceSourceType.HIGHLIGHT, 7L)));

        assertThat(EvidenceService.dedupeKey(oneSource)).isEqualTo(EvidenceService.dedupeKey(twoSources));
    }

    @Test
    @DisplayName("analyzerVersion 不参与指纹：换个分析器版本重跑不会fake出一条新证据")
    void analyzerVersionIsNotPartOfTheKey() {
        Draft v1 = new Draft(1L, 23L, 500L, EvidenceType.OBSERVATION, "他在讨论 TLB",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v1",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L)));
        Draft v2 = new Draft(1L, 23L, 500L, EvidenceType.OBSERVATION, "他在讨论 TLB",
                EvidenceCorrectness.UNKNOWN, null, null, null, false, 1L, "rules-v2",
                List.of(new SourceRef(EvidenceSourceType.MESSAGE, 88L)));

        assertThat(EvidenceService.dedupeKey(v1)).isEqualTo(EvidenceService.dedupeKey(v2));
    }

    // ================= 形状 =================

    @Test
    @DisplayName("指纹是 64 位小写 hex —— CHAR(64) 列装得下，也和大写实现区分得开")
    void keyIsLowercaseSha256Hex() {
        String key = EvidenceService.dedupeKey(draft(1L, "他在讨论 TLB"));

        assertThat(key).hasSize(64).matches("[0-9a-f]{64}");
    }
}
