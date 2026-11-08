package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.session.SessionRequest;
import com.ails.backend.dto.session.SessionResponse;
import com.ails.backend.dto.session.SessionUpdateRequest;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.LearningSession;
import com.ails.backend.enums.SessionStatus;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.LearningSessionMapper;
import com.ails.backend.mapper.LearningSpaceMapper;
import com.ails.backend.model.SessionResolver;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Objects;

/**
 * 学习活动的生命周期：懒创建、归属、状态迁移。
 * <p>
 * 判定「归入哪个 Session」的规则本身不在这里，而在纯函数
 * {@link SessionResolver}。本类只负责三件事：
 * <ol>
 *   <li>把推导规则需要的输入查出来（候选 Session）；</li>
 *   <li>把结论写回去（给 {@code conversation.session_id} 赋值、建新 Session）；</li>
 *   <li>维护 {@code last_active_at} 这类时间戳。</li>
 * </ol>
 * 这样规则可以脱离 Spring 和数据库单独测，而本类里剩下的是「怎么读写」而不是「怎么判断」。
 * <p>
 * <b>Session 不承载长期认知。</b> Evidence / Understanding / Memory 都不在这里 ——
 * Session 是「一段连续学习活动」这个活动本身，见 {@code docs/session-design.md} §二。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class SessionService {

    private final LearningSessionMapper sessionMapper;
    private final ConversationMapper conversationMapper;
    private final LearningSpaceMapper spaceMapper;
    private final LearningSpaceService spaceService;

    /**
     * 懒创建入口：给一条还没有学习活动的 Conversation 决定归属。
     * <p>
     * <b>幂等</b>：{@code conversation.sessionId} 一旦有值就直接返回，不再重新判定。
     * 这个判断刻意放在本方法内部而不是调用方 —— 归属只能决定一次，
     * 把它交给每个调用方去记着，迟早会有一处漏掉，于是同一条对话在一次学习中途换了 Session，
     * 历史证据被撕成两半。
     * <p>
     * {@code detectedConceptNames} 为空时 {@link SessionResolver} 给出 NONE，
     * 本方法不做任何写入，{@code session_id} 保持 NULL（闲聊的正常结果，不是异常）。
     *
     * @param detectedConceptNames 本轮命中的 Concept 名称，可为空
     * @return 这条对话现在归属的学习活动；{@code null} 表示没有（NONE 分支，或会话早已定好却已被删除）。
     *         调用方用它往 Agent 上下文里塞「本阶段学习目标」，所以这里返回整个对象而不是 void。
     */
    @Transactional
    public LearningSession resolveForConversation(Conversation conversation, List<String> detectedConceptNames) {
        if (conversation == null || conversation.getLearningSpaceId() == null) {
            return null;
        }
        if (conversation.getSessionId() != null) {
            // 已经定过归属了。这里读回来而不是返回 null —— 对绝大多数消息来说
            // 「已经归属的那个 Session」才是调用方要的东西。
            //
            // 但不能只凭 id 读：conversation.session_id 只是外键，数据库不保证这个
            // Session 与对话同用户、同 Space、且仍在有效状态。数据异常或未来新增
            // 调用方绕过创建逻辑时，直接 selectById 会把错误的学习目标注入上下文。
            return boundSession(conversation);
        }
        if (detectedConceptNames == null || detectedConceptNames.isEmpty()) {
            return null;
        }
        // 同一空间下的「第一次归属判定」必须串行：否则并发事务都看到「没有候选」，
        // 各自 CREATE 出重复 Session；同时 conversation.session_id 的外键检查会给
        // session 行加共享锁，随后 sessionMapper.updateById 再升级为排他锁，
        // 两个事务各持一把 S 锁等 X 锁 —— 实测直接死锁回滚一个请求。
        // 锁空间行而不是全局锁：不同空间互不阻塞；判定完成后立即进入写库，持锁很短。
        if (spaceMapper.lockById(conversation.getLearningSpaceId()) == null) {
            return null;
        }
        List<SessionResolver.Candidate> candidates = candidates(conversation.getUserId(),
                conversation.getLearningSpaceId());

        SessionResolver.Decision decision = SessionResolver.resolve(
                detectedConceptNames, candidates, LocalDateTime.now());

        return switch (decision.action()) {
            case NONE -> {
                log.debug("不属于任何学习活动, conversationId={}, concepts={}",
                        conversation.getId(), detectedConceptNames);
                yield null;
            }
            case ATTACH -> attach(conversation, decision.sessionId());
            case CREATE -> create(conversation, decision, detectedConceptNames);
        };
    }

    /** 当前学习活动：某空间下最近活跃的那一条。{@code spaceId} 可空，空则跨空间取最近的。 */
    @Transactional(readOnly = true)
    public SessionResponse current(Long userId, Long spaceId) {
        return SessionResponse.from(sessionMapper.selectOne(new LambdaQueryWrapper<LearningSession>()
                .eq(LearningSession::getUserId, userId)
                .eq(spaceId != null, LearningSession::getLearningSpaceId, spaceId)
                .in(LearningSession::getStatus, SessionStatus.ACTIVE, SessionStatus.PAUSED)
                .orderByDesc(LearningSession::getLastActiveAt)
                .last("LIMIT 1")));
    }

    @Transactional(readOnly = true)
    public List<SessionResponse> list(Long userId, Long spaceId, SessionStatus status) {
        return sessionMapper.selectList(new LambdaQueryWrapper<LearningSession>()
                        .eq(LearningSession::getUserId, userId)
                        .eq(spaceId != null, LearningSession::getLearningSpaceId, spaceId)
                        .eq(status != null, LearningSession::getStatus, status)
                        .orderByDesc(LearningSession::getLastActiveAt))
                .stream().map(SessionResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public SessionResponse get(Long userId, Long sessionId) {
        return SessionResponse.from(ownedSession(userId, sessionId));
    }

    /** 用户主动「另起一段」：显式开一个新的学习目标（§七.3） */
    @Transactional
    public SessionResponse start(Long userId, SessionRequest request) {
        spaceService.ownedSpace(userId, request.learningSpaceId());
        LearningSession session = new LearningSession();
        session.setUserId(userId);
        session.setLearningSpaceId(request.learningSpaceId());
        session.setGoal(request.goal().trim());
        session.setTitle(StringUtils.hasText(request.title())
                ? request.title().trim() : request.goal().trim());
        session.setStatus(SessionStatus.ACTIVE);
        session.setStartedAt(LocalDateTime.now());
        session.setLastActiveAt(session.getStartedAt());
        sessionMapper.insert(session);
        return SessionResponse.from(session);
    }

    @Transactional
    public SessionResponse update(Long userId, Long sessionId, SessionUpdateRequest request) {
        LearningSession session = ownedSession(userId, sessionId);
        if (StringUtils.hasText(request.title())) {
            session.setTitle(request.title().trim());
        }
        if (StringUtils.hasText(request.goal())) {
            session.setGoal(request.goal().trim());
        }
        // currentFocus 是唯一能「清空」的字段：传 "" 表示这一段不再卡在这里了。
        // 只用 hasText 判断的话没法区分「不改」和「清空」，焦点会永久粘着。
        if (request.currentFocus() != null) {
            session.setCurrentFocus(StringUtils.hasText(request.currentFocus())
                    ? request.currentFocus().trim() : null);
        }
        if (request.status() != null) {
            applyStatus(session, request.status());
        }
        sessionMapper.updateById(session);
        return SessionResponse.from(session);
    }

    /** 学习目标达成。认知（Evidence / Understanding / Memory）全部留在原处，不受影响。 */
    @Transactional
    public SessionResponse complete(Long userId, Long sessionId) {
        LearningSession session = ownedSession(userId, sessionId);
        applyStatus(session, SessionStatus.COMPLETED);
        sessionMapper.updateById(session);
        return SessionResponse.from(session);
    }

    // ================= 内部工具 =================

    /**
     * 状态迁移。
     * <p>
     * 只维护两个时间戳：{@code completed_at} 在进入 COMPLETED 时落、离开时清掉。
     * <b>不按时间自动完成或自动放弃</b> —— 这是 §八 的核心：Session 的边界是学习目标，不是时间。
     * 一周没碰只意味着它可能该 PAUSED，绝不意味着「学完了」。
     */
    private static void applyStatus(LearningSession session, SessionStatus status) {
        session.setStatus(status);
        session.setCompletedAt(status == SessionStatus.COMPLETED ? LocalDateTime.now() : null);
        session.setLastActiveAt(LocalDateTime.now());
    }

    /** 候选：本 user + 本 space + 未结束（ACTIVE / PAUSED），按最近活跃倒序，见 {@link SessionResolver} */
    private List<SessionResolver.Candidate> candidates(Long userId, Long spaceId) {
        return sessionMapper.selectList(new LambdaQueryWrapper<LearningSession>()
                        .eq(LearningSession::getUserId, userId)
                        .eq(LearningSession::getLearningSpaceId, spaceId)
                        .in(LearningSession::getStatus, SessionStatus.ACTIVE, SessionStatus.PAUSED)
                        .orderByDesc(LearningSession::getLastActiveAt))
                .stream()
                .map(s -> new SessionResolver.Candidate(s.getId(), s.getTitle(), s.getGoal(),
                        s.getCurrentFocus(), s.getLastActiveAt(), s.getStatus() == SessionStatus.ACTIVE))
                .toList();
    }

    private LearningSession attach(Conversation conversation, Long sessionId) {
        LearningSession session = sessionMapper.selectById(sessionId);
        if (session == null) {
            // 同事务里查不到，只能是并发删除之类的事 —— 不回滚整个学习分析，
            // 否则一条对话的归属问题会让本轮证据一起丢失
            log.warn("SessionResolver 给出了 ATTACH，但 session 不存在, conversationId={}, sessionId={}",
                    conversation.getId(), sessionId);
            return null;
        }
        session.setLastActiveAt(LocalDateTime.now());
        if (session.getStatus() == SessionStatus.PAUSED) {
            // 又在这里学起来了，就不是「暂停」了（§十一：ATTACH → ACTIVE）
            session.setStatus(SessionStatus.ACTIVE);
        }
        // 先写 session，再写 conversation.session_id。
        // 反过来的话，conversation 的外键检查会先给 session 行加 S 锁，紧接着
        // sessionMapper.updateById 又要升级成 X 锁；两个并发 ATTACH 各持 S、互等 X，
        // 就是 FK 检查引发的经典死锁。固定成「先 X 后 FK」的顺序后不会再出现。
        sessionMapper.updateById(session);

        conversation.setSessionId(sessionId);
        conversationMapper.updateById(conversation);
        return session;
    }

    private LearningSession create(Conversation conversation, SessionResolver.Decision decision,
                                   List<String> detectedConceptNames) {
        LearningSession session = new LearningSession();
        session.setUserId(conversation.getUserId());
        session.setLearningSpaceId(conversation.getLearningSpaceId());
        session.setTitle(decision.title());
        session.setGoal(decision.goal());
        session.setStatus(SessionStatus.ACTIVE);
        session.setStartedAt(LocalDateTime.now());
        session.setLastActiveAt(session.getStartedAt());
        sessionMapper.insert(session);

        conversation.setSessionId(session.getId());
        conversationMapper.updateById(conversation);
        log.info("新建学习活动, sessionId={}, conversationId={}, concepts={}",
                session.getId(), conversation.getId(), detectedConceptNames);
        return session;
    }

    private LearningSession ownedSession(Long userId, Long sessionId) {
        LearningSession session = sessionMapper.selectById(sessionId);
        if (session == null || !Objects.equals(session.getUserId(), userId)) {
            throw BusinessException.notFound("学习活动不存在");
        }
        return session;
    }

    /**
     * 读取对话已绑定的 Session，并校验它仍属于这条对话。
     * <p>
     * 只按 id 读是不够的：{@code conversation.session_id} 的外键只保证 Session 存在，
     * 不保证它与对话同用户、同 Space，或在有效状态。这里<b>不回退到候选逻辑</b> ——
     * 归属一旦写下就不可重新判定（见 {@link #resolveForConversation} 的幂等约定），
     * 回退会在一次学习中途改写归属；校验失败时返回 {@code null}，宁可本轮不注入
     * 学习目标，也不把错误目标交给 Agent。
     */
    private LearningSession boundSession(Conversation conversation) {
        LearningSession session = sessionMapper.selectById(conversation.getSessionId());
        if (session == null) {
            return null;
        }
        boolean sameOwner = Objects.equals(session.getUserId(), conversation.getUserId());
        boolean sameSpace = Objects.equals(
                session.getLearningSpaceId(), conversation.getLearningSpaceId());
        boolean unfinished = session.getStatus() == SessionStatus.ACTIVE
                || session.getStatus() == SessionStatus.PAUSED;
        if (!sameOwner || !sameSpace || !unfinished) {
            log.warn("已绑定学习活动与对话归属不一致，拒绝注入, conversationId={}, sessionId={}, "
                            + "conversationUserId={}, sessionUserId={}, conversationSpaceId={}, "
                            + "sessionSpaceId={}, sessionStatus={}",
                    conversation.getId(), conversation.getSessionId(),
                    conversation.getUserId(), session.getUserId(),
                    conversation.getLearningSpaceId(), session.getLearningSpaceId(),
                    session.getStatus());
            return null;
        }
        return session;
    }
}
