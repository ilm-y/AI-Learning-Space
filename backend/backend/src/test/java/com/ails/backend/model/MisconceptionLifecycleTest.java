package com.ails.backend.model;

import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.MisconceptionStatus;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 误区生命周期的规则测试。
 * <p>
 * {@link MisconceptionLifecycle} 是纯函数，不碰数据库，所以这里不需要 Spring 上下文，
 * 也不需要 MySQL —— 规则本身可以单独回归。
 */
class MisconceptionLifecycleTest {

    private static final LocalDateTime T0 = LocalDateTime.of(2026, 9, 10, 10, 0);

    @Test
    @DisplayName("错误证据让误区进入 ACTIVE")
    void incorrectEvidenceActivatesMisconception() {
        List<Misconception> result = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "把三次握手当成数据可靠传输"));

        assertThat(result).hasSize(1);
        assertThat(result.get(0).text()).isEqualTo("把三次握手当成数据可靠传输");
        assertThat(result.get(0).status()).isEqualTo(MisconceptionStatus.ACTIVE);
        assertThat(result.get(0).firstDetectedAt()).isEqualTo(evidenceTime(1L));
        assertThat(result.get(0).reversalEvidenceIds()).isEmpty();
        assertThat(MisconceptionLifecycle.severity(result)).isEqualTo(MisconceptionStatus.ACTIVE);
    }

    @Test
    @DisplayName("一次正确证据只降到 PENDING，不直接解除")
    void firstCorrectEvidenceMovesToPending() {
        List<Misconception> active = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));

        List<Misconception> result = MisconceptionLifecycle.evolve(
                active, evidence(2L, EvidenceCorrectness.CORRECT, 0.9f, "正确解释"));

        assertThat(result).hasSize(1);
        assertThat(result.get(0).status()).isEqualTo(MisconceptionStatus.PENDING);
        assertThat(result.get(0).reversalEvidenceIds()).containsExactly(2L);
        // 仍然是 MISCONCEPTION 状态，只是 attention 降级
        assertThat(MisconceptionLifecycle.hasUnresolved(result)).isTrue();
        assertThat(MisconceptionLifecycle.severity(result)).isEqualTo(MisconceptionStatus.PENDING);
    }

    @Test
    @DisplayName("第二次独立正确证据解除误区，条目从当前快照消失")
    void secondCorrectEvidenceResolvesMisconception() {
        List<Misconception> active = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));
        List<Misconception> pending = MisconceptionLifecycle.evolve(
                active, evidence(2L, EvidenceCorrectness.CORRECT, 0.9f, "解释一"));

        List<Misconception> result = MisconceptionLifecycle.evolve(
                pending, evidence(3L, EvidenceCorrectness.CORRECT, 0.9f, "解释二"));

        assertThat(result).isEmpty();
        assertThat(MisconceptionLifecycle.hasUnresolved(result)).isFalse();
        assertThat(MisconceptionLifecycle.severity(result)).isNull();
    }

    @Test
    @DisplayName("同一条正确证据重复投递不会重复计数")
    void sameEvidenceAppliedTwiceDoesNotDoubleCount() {
        List<Misconception> active = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));
        LearningEvidence correct = evidence(2L, EvidenceCorrectness.CORRECT, 0.9f, "正确解释");

        List<Misconception> once = MisconceptionLifecycle.evolve(active, correct);
        List<Misconception> twice = MisconceptionLifecycle.evolve(once, correct);

        assertThat(twice).isEqualTo(once);
        assertThat(twice.get(0).status()).isEqualTo(MisconceptionStatus.PENDING);
    }

    @Test
    @DisplayName("推理质量不够的反向证据不予采信")
    void weakReasoningDoesNotReverse() {
        List<Misconception> active = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));

        List<Misconception> result = MisconceptionLifecycle.evolve(
                active, evidence(2L, EvidenceCorrectness.CORRECT, 0.1f, "含糊其辞"));

        assertThat(result.get(0).status()).isEqualTo(MisconceptionStatus.ACTIVE);
        assertThat(result.get(0).reversalEvidenceIds()).isEmpty();
    }

    @Test
    @DisplayName("解除之后又出现错误证据，重新激活并重新计时")
    void resolvedMisconceptionCanReactivate() {
        List<Misconception> active = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));
        List<Misconception> pending = MisconceptionLifecycle.evolve(
                active, evidence(2L, EvidenceCorrectness.CORRECT, 0.9f, "解释一"));
        List<Misconception> resolved = MisconceptionLifecycle.evolve(
                pending, evidence(3L, EvidenceCorrectness.CORRECT, 0.9f, "解释二"));
        assertThat(resolved).isEmpty();

        List<Misconception> again = MisconceptionLifecycle.evolve(
                resolved, evidence(4L, EvidenceCorrectness.INCORRECT, null, "误解题"));

        assertThat(again).hasSize(1);
        assertThat(again.get(0).status()).isEqualTo(MisconceptionStatus.ACTIVE);
        // 上一轮已经解除、条目已被移除，所以这一轮从新证据的时间重新计时
        assertThat(again.get(0).firstDetectedAt()).isEqualTo(evidenceTime(4L));
        assertThat(again.get(0).reversalEvidenceIds()).isEmpty();
    }

    @Test
    @DisplayName("PARTIAL / UNKNOWN 证据不参与误区判定")
    void partialAndUnknownDoNotAffectMisconceptions() {
        List<Misconception> active = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));

        assertThat(MisconceptionLifecycle.evolve(
                active, evidence(2L, EvidenceCorrectness.PARTIAL, 0.9f, "答了一半"))).isEqualTo(active);
        assertThat(MisconceptionLifecycle.evolve(
                active, evidence(3L, EvidenceCorrectness.UNKNOWN, null, "只是提到"))).isEqualTo(active);
    }

    @Test
    @DisplayName("没有误区时正确证据不凭空造出条目")
    void correctEvidenceWithoutMisconceptionIsNoop() {
        List<Misconception> result = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.CORRECT, 0.9f, "正确解释"));

        assertThat(result).isEmpty();
    }

    @Test
    @DisplayName("快照 JSON 可以往返序列化")
    void serializationRoundTrip() {
        List<Misconception> list = MisconceptionLifecycle.evolve(
                List.of(), evidence(1L, EvidenceCorrectness.INCORRECT, null, "误解题"));

        String json = MisconceptionLifecycle.serialize(list);

        assertThat(json).contains("\"status\":\"ACTIVE\"");
        assertThat(MisconceptionLifecycle.parse(json)).isEqualTo(list);
        assertThat(MisconceptionLifecycle.parse(null)).isEmpty();
        assertThat(MisconceptionLifecycle.parse("")).isEmpty();
    }

    /** 每条证据按 id 递增 1 分钟，模拟真实的时间先后 */
    private static LocalDateTime evidenceTime(Long id) {
        return T0.plusMinutes(id);
    }

    private static LearningEvidence evidence(Long id, EvidenceCorrectness correctness,
                                             Float reasoningQuality, String observation) {
        LearningEvidence evidence = new LearningEvidence();
        evidence.setId(id);
        evidence.setCorrectness(correctness);
        evidence.setReasoningQuality(reasoningQuality);
        evidence.setObservation(observation);
        evidence.setCreatedAt(evidenceTime(id));
        return evidence;
    }
}
