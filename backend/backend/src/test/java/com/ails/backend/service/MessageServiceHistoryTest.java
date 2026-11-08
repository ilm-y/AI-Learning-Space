package com.ails.backend.service;

import com.ails.backend.dto.agent.ChatHistoryMessage;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.MessageMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 历史消息的服务层契约：<b>把 userId 一并交给 SQL</b>。
 * <p>
 * 归属过滤本身由 {@link com.ails.backend.mapper.MessageHistorySqlTest} 固定；
 * 这里只防止未来重构把 userId 参数丢掉、退回到「只按 conversationId 查」。
 */
class MessageServiceHistoryTest {

    private static final long USER_ID = 1L;
    private static final long OTHER_USER_ID = 2L;
    private static final long CONVERSATION_ID = 10L;
    private static final long EXCLUDED_MESSAGE_ID = 99L;

    private MessageMapper messageMapper;
    private MessageService service;

    @BeforeEach
    void setUp() {
        messageMapper = mock(MessageMapper.class);
        service = new MessageService(
                messageMapper,
                mock(ConversationMapper.class),
                mock(ConversationService.class),
                mock(CitationService.class));
    }

    @Test
    @DisplayName("历史查询把 userId 传给 SQL，并把倒序结果翻正为时间正序")
    void passesUserIdAndReversesChronologicalOrder() {
        Message first = message(MessageRole.USER, "问题");
        Message second = message(MessageRole.ASSISTANT, "回答");
        // Mapper 返回的是「最近 N 条」的倒序结果。
        when(messageMapper.selectHistoryForUser(
                USER_ID, CONVERSATION_ID, List.of(EXCLUDED_MESSAGE_ID), 20))
                .thenReturn(List.of(second, first));

        List<ChatHistoryMessage> result = service.history(
                USER_ID, CONVERSATION_ID, EXCLUDED_MESSAGE_ID, 20);

        assertThat(result).extracting(ChatHistoryMessage::content)
                .containsExactly("问题", "回答");
        verify(messageMapper).selectHistoryForUser(
                USER_ID, CONVERSATION_ID, List.of(EXCLUDED_MESSAGE_ID), 20);
    }

    @Test
    @DisplayName("userId 与对话不匹配时返回空：归属过滤由 SQL 的 conversation.user_id 完成")
    void mismatchedUserYieldsNoMessages() {
        when(messageMapper.selectHistoryForUser(
                OTHER_USER_ID, CONVERSATION_ID, List.of(), 20))
                .thenReturn(List.of());

        assertThat(service.history(OTHER_USER_ID, CONVERSATION_ID, (Long) null, 20)).isEmpty();
        verify(messageMapper).selectHistoryForUser(
                OTHER_USER_ID, CONVERSATION_ID, List.of(), 20);
    }

    private static Message message(MessageRole role, String content) {
        Message message = new Message();
        message.setRole(role);
        message.setContent(content);
        return message;
    }
}
