package com.ails.backend.service;

import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.LearningSession;
import com.ails.backend.enums.SessionStatus;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.LearningSessionMapper;
import com.ails.backend.mapper.LearningSpaceMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 对话已绑定 Session 的读取边界。
 * <p>
 * {@code conversation.session_id} 只是外键，不保证 Session 与该对话同用户、同 Space
 * 或仍处于有效状态。这里固定「数据异常时宁可不注入，也不返回错误 Session」，
 * 并确认校验失败不会误入候选/创建路径（那会中途改写归属）。
 */
@ExtendWith(MockitoExtension.class)
class SessionServiceTest {

    private static final long USER_ID = 1L;
    private static final long OTHER_USER_ID = 2L;
    private static final long SPACE_ID = 7L;
    private static final long OTHER_SPACE_ID = 8L;
    private static final long SESSION_ID = 20L;
    private static final long CONVERSATION_ID = 30L;

    @Mock
    private LearningSessionMapper sessionMapper;
    @Mock
    private ConversationMapper conversationMapper;
    @Mock
    private LearningSpaceMapper spaceMapper;
    @Mock
    private LearningSpaceService spaceService;

    private SessionService service;

    @BeforeEach
    void setUp() {
        service = new SessionService(sessionMapper, conversationMapper, spaceMapper, spaceService);
    }

    @Test
    @DisplayName("已绑定 Session 与对话同用户、同 Space 且 ACTIVE：正常返回")
    void returnsMatchingActiveSession() {
        when(sessionMapper.selectById(SESSION_ID)).thenReturn(session(USER_ID, SPACE_ID, SessionStatus.ACTIVE));

        LearningSession result = service.resolveForConversation(conversation(), null);

        assertThat(result).isNotNull();
        assertThat(result.getId()).isEqualTo(SESSION_ID);
    }

    @Test
    @DisplayName("已绑定 Session 属于其他用户：拒绝注入，且不回退到候选逻辑")
    void rejectsSessionOwnedByAnotherUser() {
        when(sessionMapper.selectById(SESSION_ID))
                .thenReturn(session(OTHER_USER_ID, SPACE_ID, SessionStatus.ACTIVE));

        LearningSession result = service.resolveForConversation(conversation(), null);

        assertThat(result).isNull();
        // 不回退到候选路径：归属一旦写下就不再重新判定，避免中途改写 session_id。
        verify(sessionMapper, never()).selectList(any());
        verify(spaceMapper, never()).lockById(any());
    }

    @Test
    @DisplayName("已绑定 Session 属于其他 Space：拒绝注入")
    void rejectsSessionFromAnotherSpace() {
        when(sessionMapper.selectById(SESSION_ID))
                .thenReturn(session(USER_ID, OTHER_SPACE_ID, SessionStatus.ACTIVE));

        assertThat(service.resolveForConversation(conversation(), null)).isNull();
    }

    @Test
    @DisplayName("已绑定 Session 已完成：拒绝把旧目标当成当前学习框架")
    void rejectsCompletedSession() {
        when(sessionMapper.selectById(SESSION_ID))
                .thenReturn(session(USER_ID, SPACE_ID, SessionStatus.COMPLETED));

        assertThat(service.resolveForConversation(conversation(), null)).isNull();
    }

    @Test
    @DisplayName("PAUSED 仍属于未完成状态：可以继续作为当前学习活动")
    void pausedSessionIsStillValid() {
        when(sessionMapper.selectById(SESSION_ID))
                .thenReturn(session(USER_ID, SPACE_ID, SessionStatus.PAUSED));

        assertThat(service.resolveForConversation(conversation(), null)).isNotNull();
    }

    private static Conversation conversation() {
        Conversation conversation = new Conversation();
        conversation.setId(CONVERSATION_ID);
        conversation.setUserId(USER_ID);
        conversation.setLearningSpaceId(SPACE_ID);
        conversation.setSessionId(SESSION_ID);
        return conversation;
    }

    private static LearningSession session(long userId, long spaceId, SessionStatus status) {
        LearningSession session = new LearningSession();
        session.setId(SESSION_ID);
        session.setUserId(userId);
        session.setLearningSpaceId(spaceId);
        session.setStatus(status);
        return session;
    }
}
