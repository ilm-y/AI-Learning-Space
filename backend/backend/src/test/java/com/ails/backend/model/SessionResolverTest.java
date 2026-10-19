package com.ails.backend.model;

import com.ails.backend.model.SessionResolver.Action;
import com.ails.backend.model.SessionResolver.Candidate;
import com.ails.backend.model.SessionResolver.Decision;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Session Resolver 的规则测试。
 * <p>
 * {@link SessionResolver} 是纯函数，所以这里不起 Spring 上下文、不连 MySQL ——
 * 归属规则本身可以单独回归。四种判定（NONE / 文本命中 ATTACH / 最近 ATTACH / CREATE）
 * 各有用例，NONE 那一条尤其重要：它是 {@code conversation.session_id} 可以为 NULL 的
 * 第二种合理业务状态，混掉之后 NULL 就变成无法解释的脏数据了。
 */
class SessionResolverTest {

    /** 固定参考时刻：时间由调用方传入正是为了能这样钉住「7 天内」这条规则 */
    private static final LocalDateTime NOW = LocalDateTime.of(2026, 9, 20, 10, 0);

    private static Candidate candidate(Long id, String goal, LocalDateTime lastActiveAt, boolean active) {
        return new Candidate(id, "标题 " + id, goal, null, lastActiveAt, active);
    }

    // ================= 规则 1：NONE =================

    @Test
    @DisplayName("没有命中的 Concept → NONE，不创建学习活动")
    void emptyConceptsYieldNone() {
        Decision decision = SessionResolver.resolve(List.of(), List.of(), NOW);

        assertThat(decision.action()).isEqualTo(Action.NONE);
        assertThat(decision.sessionId()).isNull();
    }

    @Test
    @DisplayName("没有命中的 Concept 时，即使有候选 Session 也仍然是 NONE")
    void emptyConceptsYieldNoneEvenWithCandidates() {
        // 闲聊不该被硬塞进正在进行的学习活动里 —— 这是 NONE 优先级最高的意义
        Decision decision = SessionResolver.resolve(
                List.of(),
                List.of(candidate(20L, "理解虚拟内存地址转换", NOW.minusHours(1), true)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.NONE);
        assertThat(decision.sessionId()).isNull();
    }

    @Test
    @DisplayName("null 输入按空处理，不抛异常")
    void nullInputsAreTolerated() {
        assertThat(SessionResolver.resolve(null, null, NOW).action()).isEqualTo(Action.NONE);
        assertThat(SessionResolver.resolve(List.of("TLB"), null, NOW).action()).isEqualTo(Action.CREATE);
    }

    // ================= 规则 2：文本命中 → ATTACH =================

    @Test
    @DisplayName("候选 Session 的目标命中了本轮概念 → 归入它")
    void goalMatchAttaches() {
        Decision decision = SessionResolver.resolve(
                List.of("TLB"),
                List.of(candidate(20L, "理解 TLB 与 Page Table 的关系", NOW.minusDays(30), false)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.ATTACH);
        assertThat(decision.sessionId()).isEqualTo(20L);
    }

    @Test
    @DisplayName("PAUSED 的候选也能被文本命中（暂停不等于结束，旧目标仍然相关）")
    void pausedCandidateCanBeAttachedByText() {
        Decision decision = SessionResolver.resolve(
                List.of("TLB"),
                List.of(candidate(20L, "理解 TLB", NOW.minusDays(90), false)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.ATTACH);
        assertThat(decision.sessionId()).isEqualTo(20L);
    }

    @Test
    @DisplayName("大小写不敏感：tlb 与 TLB 是同一个概念")
    void textMatchIsCaseInsensitive() {
        Decision decision = SessionResolver.resolve(
                List.of("tlb"),
                List.of(candidate(20L, "理解 TLB 与 Page Table", NOW.minusDays(30), false)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.ATTACH);
    }

    @Test
    @DisplayName("current_focus 也算命中：焦点谈到的就是当前在做的事")
    void currentFocusMatchAttaches() {
        Candidate withFocus = new Candidate(20L, "虚拟内存", "理解地址转换", "卡在 TLB Miss 上",
                NOW.minusDays(30), false);

        Decision decision = SessionResolver.resolve(List.of("TLB Miss"), List.of(withFocus), NOW);

        assertThat(decision.action()).isEqualTo(Action.ATTACH);
        assertThat(decision.sessionId()).isEqualTo(20L);
    }

    @Test
    @DisplayName("文本命中优先于「最近活跃」：旧但相关的目标赢过新但不相关的")
    void textMatchWinsOverRecency() {
        Decision decision = SessionResolver.resolve(
                List.of("inode"),
                List.of(
                        candidate(21L, "理解 inode 与文件系统", NOW.minusDays(100), true),
                        candidate(20L, "理解虚拟内存地址转换", NOW.minusMinutes(5), true)),
                NOW);

        // 两个都 ACTIVE，靠文本命中挑出 21，而不是靠 last_active_at 挑出 20
        assertThat(decision.action()).isEqualTo(Action.ATTACH);
        assertThat(decision.sessionId()).isEqualTo(21L);
    }

    // ================= 规则 3：唯一且最近的 ACTIVE → ATTACH =================

    @Test
    @DisplayName("只有一个 ACTIVE 候选且在 7 天内 → 默认接续它")
    void singleRecentActiveCandidateAttaches() {
        Decision decision = SessionResolver.resolve(
                List.of("页表"),
                List.of(candidate(20L, "理解虚拟内存地址转换", NOW.minusDays(3), true)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.ATTACH);
        assertThat(decision.sessionId()).isEqualTo(20L);
    }

    @Test
    @DisplayName("超过 7 天没碰、又没文本命中 → 开新的，不自动复活旧目标")
    void staleCandidateCreatesNew() {
        Decision decision = SessionResolver.resolve(
                List.of("页表"),
                List.of(candidate(20L, "理解虚拟内存地址转换", NOW.minusDays(8), true)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.CREATE);
    }

    @Test
    @DisplayName("唯一候选是 PAUSED → 不靠「最近」选中它，开新的")
    void singlePausedCandidateDoesNotAttachByRecency() {
        Decision decision = SessionResolver.resolve(
                List.of("页表"),
                List.of(candidate(20L, "理解虚拟内存地址转换", NOW.minusMinutes(1), false)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.CREATE);
    }

    @Test
    @DisplayName("两个 ACTIVE 候选都相关不上 → 不猜哪个，开新的")
    void ambiguousActiveCandidatesCreateNew() {
        Decision decision = SessionResolver.resolve(
                List.of("页表"),
                List.of(
                        candidate(20L, "理解虚拟内存地址转换", NOW.minusHours(1), true),
                        candidate(21L, "理解进程调度", NOW.minusHours(2), true)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.CREATE);
    }

    @Test
    @DisplayName("last_active_at 为空时「最近」这条规则不采信它")
    void nullLastActiveAtIsNotRecent() {
        Decision decision = SessionResolver.resolve(
                List.of("页表"),
                List.of(candidate(20L, "理解虚拟内存地址转换", null, true)),
                NOW);

        assertThat(decision.action()).isEqualTo(Action.CREATE);
    }

    // ================= 规则 4：CREATE 带上目标与标题 =================

    @Test
    @DisplayName("CREATE 用命中的概念生成标题与目标")
    void createCarriesTitleAndGoal() {
        Decision decision = SessionResolver.resolve(List.of("TLB", "Page Table"), List.of(), NOW);

        assertThat(decision.action()).isEqualTo(Action.CREATE);
        assertThat(decision.sessionId()).isNull();
        assertThat(decision.title()).isEqualTo("TLB、Page Table");
        assertThat(decision.goal()).contains("TLB、Page Table");
    }

    @Test
    @DisplayName("CREATE 去重概念名，不重复列同一个")
    void createDeduplicatesConceptNames() {
        Decision decision = SessionResolver.resolve(List.of("TLB", "TLB"), List.of(), NOW);

        assertThat(decision.title()).isEqualTo("TLB");
    }

    @Test
    @DisplayName("概念名全是空白时退化成一个有内容的兜底标题，不能是空串")
    void createFallsBackWhenNamesAreBlank() {
        Decision decision = SessionResolver.resolve(List.of("  "), List.of(), NOW);

        assertThat(decision.action()).isEqualTo(Action.CREATE);
        assertThat(decision.title()).isNotBlank();
    }
}
