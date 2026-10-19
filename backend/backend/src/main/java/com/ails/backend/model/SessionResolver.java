package com.ails.backend.model;

import org.springframework.util.StringUtils;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Locale;

/**
 * Session Resolver —— 决定一条 Conversation 该归入哪个学习活动。纯函数，不碰数据库，可以单独测。
 * <p>
 * 背景见 {@code docs/session-design.md}：Conversation 创建时 {@code session_id} 是 NULL，
 * 直到用户发出第一条<b>有效学习消息</b>（= 首次抽出至少一个 Concept），才由本类决定
 * 「加入已有 Session」还是「开一个新的」。
 *
 * <h2>输入契约（钉死的部分，改动前先看这里）</h2>
 * <pre>
 *   detectedConcepts : 本轮命中的 Concept 名称，<b>可为空</b>
 *   candidates       : 本 user + 本 space + status ∈ (ACTIVE, PAUSED) 的已有 Session，
 *                      按 last_active_at 倒序
 *   now              : 参考时刻，由调用方传入（而不是内部取系统时间）—— 否则「7 天内」这条规则没法测
 * </pre>
 * userId / spaceId / conversationId 不参与判定，所以不是本类的入参：它们由调用方
 * （{@code SessionService}）用来把 candidates 查出来、把结论写回去。
 *
 * <h2>输出契约</h2>
 * <pre>
 *   Decision { action: ATTACH | CREATE | NONE, sessionId: Long?, title: String?, goal: String? }
 * </pre>
 *
 * <h2>判定规则（按优先级，第一条命中即停）</h2>
 * <ol>
 *   <li>{@code detectedConcepts} 为空 → <b>NONE</b>。不创建 Session，{@code conversation.session_id}
 *       保持 NULL。闲聊、问一个全新的知识点都属于这一类，不该产生学习活动。</li>
 *   <li>某个 candidate 的 title / goal / current_focus 文本命中任一 detectedConcept → <b>ATTACH</b> 它。</li>
 *   <li>恰好只有一个 ACTIVE candidate，且它的 last_active_at 在 {@link #RECENT_WINDOW} 之内 → <b>ATTACH</b> 它。</li>
 *   <li>其余 → <b>CREATE</b>，title / goal 由命中的 Concept 生成。</li>
 * </ol>
 *
 * <b>NONE 是显式的一档，不是「没想好」。</b> 它是 {@code conversation.session_id} 可以为 NULL 的
 * 第二种合理业务状态（第一种是「建了对话还没说话」，见 session-design.md §四）。
 * 把「不创建」和「创建失败」混成一个结果，会让 NULL 变成无法解释的脏数据。
 *
 * <h2>为什么用「文本命中」而不是「会话内 Evidence 的 Concept 交集」</h2>
 * 后者更精确，但要 join conversation → message → evidence → concept 四张表，且要在每次
 * 首条消息时跑一遍。规则在这里是纯函数，将来想升级成交集判定，只改 {@link #matches} 一处，
 * 数据模型和调用方都不受影响。
 */
public final class SessionResolver {

    /**
     * 规则 3 的「最近」窗口：一周。
     * <p>
     * 一周没碰的 Session 不该被自动当成完成（那会丢目标），也不该被无脑复活
     * （用户可能已经在做别的事了）。超出这个窗口又没有文本命中，就开新的。
     */
    public static final Duration RECENT_WINDOW = Duration.ofDays(7);

    /** 自动生成的 title 长度上限，避免把一整句话塞进列表页 */
    private static final int TITLE_MAX_LENGTH = 60;

    private SessionResolver() {
    }

    /** 解析结果的动作。{@link #NONE} 表示「不归入任何学习活动」，见类注释。 */
    public enum Action {
        ATTACH,
        CREATE,
        NONE
    }

    /**
     * 一个候选 Session。
     *
     * @param sessionId    session 主键
     * @param title        标题，可空
     * @param goal         这一阶段最终要解决什么（长期），可空
     * @param currentFocus 当前卡在哪里（短期），可空
     * @param lastActiveAt 最后活跃时间，可空 —— 为空表示无从判断新旧，规则 3 不采信它
     * @param active       {@code status == ACTIVE}。PAUSED 的也能被文本命中（规则 2），
     *                     但不会靠「最近」被选中（规则 3）—— 暂停了还主动往里塞新对话是错的。
     */
    public record Candidate(Long sessionId, String title, String goal, String currentFocus,
                            LocalDateTime lastActiveAt, boolean active) {
    }

    /**
     * 判定结果。
     *
     * @param action    {@link Action}
     * @param sessionId 仅 ATTACH 时非空
     * @param title     仅 CREATE 时非空
     * @param goal      仅 CREATE 时非空
     */
    public record Decision(Action action, Long sessionId, String title, String goal) {

        static Decision attach(Long sessionId) {
            return new Decision(Action.ATTACH, sessionId, null, null);
        }

        static Decision create(String title, String goal) {
            return new Decision(Action.CREATE, null, title, goal);
        }

        static Decision none() {
            return new Decision(Action.NONE, null, null, null);
        }
    }

    /**
     * 按类注释的 4 条规则给出判定。
     *
     * @param detectedConcepts 本轮命中的 Concept 名称，可为空
     * @param candidates       已有候选，按 last_active_at 倒序（顺序不影响结果，规则 3 要求唯一）
     * @param now              参考时刻
     * @return 永不返回 null
     */
    public static Decision resolve(List<String> detectedConcepts, List<Candidate> candidates, LocalDateTime now) {
        // 规则 1：没有命中的 Concept —— 就不是学习行为，不产生学习活动
        if (detectedConcepts == null || detectedConcepts.isEmpty()) {
            return Decision.none();
        }
        List<Candidate> pool = candidates == null ? List.of() : candidates;

        // 规则 2：已有 Session 的目标/焦点谈到了本轮的概念 —— 归入它
        for (Candidate candidate : pool) {
            if (matches(candidate, detectedConcepts)) {
                return Decision.attach(candidate.sessionId());
            }
        }

        // 规则 3：只有一个正在进行的 Session 且还热着 —— 默认接续它
        List<Candidate> active = pool.stream().filter(Candidate::active).toList();
        if (active.size() == 1 && isRecent(active.get(0), now)) {
            return Decision.attach(active.get(0).sessionId());
        }

        // 规则 4：开一个新的
        return Decision.create(titleFor(detectedConcepts), goalFor(detectedConcepts));
    }

    /**
     * 规则 2 的文本命中判定。
     * <p>
     * 大小写不敏感（「TLB」与「tlb」是同一个概念），双向包含：既认
     * 「目标是虚拟内存地址转换」命中概念「虚拟内存」，也认反过来的写法。
     * 不做分词 —— 有意如此：这里要的是「看起来在讲同一件事」，
     * 漏判的后果只是多开一个 Session，误判的后果是把不相干的对话并进来，
     * 后者更糟，所以宁可判得保守一点。
     */
    private static boolean matches(Candidate candidate, List<String> detectedConcepts) {
        String haystack = join(candidate.title(), candidate.goal(), candidate.currentFocus());
        if (haystack.isEmpty()) {
            return false;
        }
        for (String concept : detectedConcepts) {
            if (!StringUtils.hasText(concept)) {
                continue;
            }
            String needle = concept.trim().toLowerCase(Locale.ROOT);
            if (haystack.contains(needle)) {
                return true;
            }
            // 反向也认：目标是「虚拟内存」、本轮概念是「虚拟内存地址转换」时算命中。
            // 但要求 haystack 至少两个字符 —— 否则目标是「图」这种单字时，
            // 任何含「图」的概念都会把它算成命中。
            if (haystack.length() >= 2 && needle.contains(haystack)) {
                return true;
            }
        }
        return false;
    }

    private static String join(String... parts) {
        StringBuilder sb = new StringBuilder();
        for (String part : parts) {
            if (StringUtils.hasText(part)) {
                if (sb.length() > 0) {
                    sb.append('\n');
                }
                sb.append(part.trim().toLowerCase(Locale.ROOT));
            }
        }
        return sb.toString();
    }

    private static boolean isRecent(Candidate candidate, LocalDateTime now) {
        return candidate.lastActiveAt() != null
                && now != null
                && !candidate.lastActiveAt().isBefore(now.minus(RECENT_WINDOW));
    }

    /** 自动 title：命中的概念名连起来。用户之后可以自己改。 */
    private static String titleFor(List<String> detectedConcepts) {
        String joined = String.join("、", detectedConcepts.stream()
                .filter(StringUtils::hasText)
                .map(String::trim)
                .distinct()
                .toList());
        if (!StringUtils.hasText(joined)) {
            return "新的学习目标";
        }
        return joined.length() <= TITLE_MAX_LENGTH ? joined : joined.substring(0, TITLE_MAX_LENGTH);
    }

    /**
     * 自动 goal：只有概念名，没有「要解决什么」。
     * <p>
     * 这是刻意的下限而不是上限 —— 生成真正的学习目标需要读懂用户的意图（那是 LLM 的活），
     * 本地规则编不出可信的句子。写成「理解 X」这类模板句反而会污染 Agent 的上下文：
     * 用户会把它当成系统确知的结论。宁可留一个诚实的起点，让用户或后续的 Agent 补全。
     */
    private static String goalFor(List<String> detectedConcepts) {
        return "围绕「" + titleFor(detectedConcepts) + "」的学习";
    }
}
