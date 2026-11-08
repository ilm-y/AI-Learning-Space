package com.ails.backend.service;

import com.ails.backend.agent.AgentGateway;
import com.ails.backend.agent.AgentStreamHandler;
import com.ails.backend.agent.StreamCompletion;
import com.ails.backend.common.ChatReplayException;
import com.ails.backend.dto.agent.AgentCitation;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.dto.conversation.ChatReplayResponse;
import com.ails.backend.dto.conversation.CitationMetadata;
import com.ails.backend.dto.conversation.CitationResponse;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.ConversationStatus;
import com.ails.backend.mapper.AgentRunMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.core.task.TaskExecutor;
import org.springframework.web.servlet.mvc.method.annotation.ResponseBodyEmitter;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * SSE 传输失败与学习分析触发之间的关系。
 * <p>
 * 固定三条约束：
 * <ol>
 *   <li>助手消息已落库后，客户端在 send / complete 阶段断开，不能跳过学习分析；</li>
 *   <li>助手消息没有落库时，不能拿内存里的 buffer 冒充已持久化消息去分析；</li>
 *   <li>传输失败不能导致消息重复保存或分析重复触发。</li>
 * </ol>
 * 这里用内联 TaskExecutor 把 streamReply 跑到当前线程，用可控的 SseEmitter
 * 模拟 Tomcat 在客户端断开时可能抛出的运行时异常；不依赖真实网络或容器。
 */
class ChatServiceTest {

    private static final long USER_ID = 1L;
    private static final long CONVERSATION_ID = 10L;
    private static final long USER_MESSAGE_ID = 100L;
    private static final long ASSISTANT_MESSAGE_ID = 101L;
    private static final long RUN_ID = 200L;
    private static final String QUESTION = "TLB 缓存的是什么？";
    private static final String ANSWER = "TLB 缓存的是页表项。";
    private static final String CLIENT_REQUEST_ID = "client-request-0001";

    private ConversationService conversationService;
    private MessageService messageService;
    private AgentGateway agentGateway;
    private AgentRunMapper agentRunMapper;
    private ChatTurnService chatTurnService;
    private AnalysisDispatchService analysisDispatchService;
    private ChatService chatService;

    private Conversation conversation;
    private Message userMessage;
    private AgentRun runningRun;
    private LearningAnalysisService.TurnOpening opening;

    @BeforeEach
    void setUp() {
        conversationService = mock(ConversationService.class);
        messageService = mock(MessageService.class);
        agentGateway = mock(AgentGateway.class);
        agentRunMapper = mock(AgentRunMapper.class);
        chatTurnService = mock(ChatTurnService.class);
        analysisDispatchService = mock(AnalysisDispatchService.class);

        conversation = new Conversation();
        conversation.setId(CONVERSATION_ID);
        conversation.setUserId(USER_ID);
        conversation.setLearningSpaceId(2L);
        conversation.setStatus(ConversationStatus.ACTIVE);

        userMessage = new Message();
        userMessage.setId(USER_MESSAGE_ID);
        userMessage.setConversationId(CONVERSATION_ID);
        userMessage.setContent(QUESTION);
        userMessage.setClientRequestId(CLIENT_REQUEST_ID);

        runningRun = new AgentRun();
        runningRun.setId(RUN_ID);
        runningRun.setConversationId(CONVERSATION_ID);
        runningRun.setUserId(USER_ID);
        runningRun.setStatus(AgentRunStatus.RUNNING);
        runningRun.setClientRequestId(CLIENT_REQUEST_ID);

        opening = new LearningAnalysisService.TurnOpening(List.of(), null);

        when(conversationService.ownedConversation(USER_ID, CONVERSATION_ID)).thenReturn(conversation);
        when(chatTurnService.findExisting(CONVERSATION_ID, CLIENT_REQUEST_ID)).thenReturn(Optional.empty());
        when(chatTurnService.prepare(conversation, QUESTION, CLIENT_REQUEST_ID))
                .thenReturn(new ChatTurnService.TurnSnapshot(userMessage, opening, runningRun, null));
        when(messageService.history(eq(CONVERSATION_ID), eq(USER_MESSAGE_ID), anyInt())).thenReturn(List.of());
    }

    @Test
    @DisplayName("正常 SSE 流：助手消息落库一次、分析触发一次")
    void normalStreamPersistsOnceAndAnalyzesOnce() {
        when(messageService.appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList()))
                .thenReturn(assistantMessage());
        handlerWithDeltaAndComplete();

        TestEmitter emitter = new TestEmitter(false, false);
        chatService = serviceWith(emitter);

        chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token");

        verify(messageService).appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList());
        verify(analysisDispatchService).dispatch(
                conversation, userMessage, ANSWER, ASSISTANT_MESSAGE_ID, "token", opening);
        assertThat(emitter.sendCount).isEqualTo(3); // start / delta / end
        assertThat(emitter.completed).isTrue();
        assertThat(completedRun().getStatus()).isEqualTo(AgentRunStatus.COMPLETED);
        verify(agentGateway, times(1)).streamReply(any(ChatAgentRequest.class), any(AgentStreamHandler.class));
        // 预创建事务只走一次；用户消息不是由 ChatService 自己重复写入的
        verify(chatTurnService, times(1)).prepare(conversation, QUESTION, CLIENT_REQUEST_ID);
        // run 上记录助手消息 id，供后续同 clientRequestId 回放定位
        assertThat(completedRun().getAssistantMessageId()).isEqualTo(ASSISTANT_MESSAGE_ID);
        verify(analysisDispatchService, times(1)).dispatch(
                any(Conversation.class), any(Message.class), any(String.class), any(Long.class),
                any(String.class), any(LearningAnalysisService.TurnOpening.class));
    }

    @Test
    @DisplayName("首个 delta 发送抛运行时异常：仍完成落库并触发分析")
    void sendFailureDoesNotSkipPersistenceOrAnalysis() {
        when(messageService.appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList()))
                .thenReturn(assistantMessage());
        handlerWithDeltaAndComplete();

        // 第 1 次 send 是 message.start，第 2 次是 message.delta
        TestEmitter emitter = new TestEmitter(true, false);
        chatService = serviceWith(emitter);

        chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token");

        verify(messageService, times(1)).appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList());
        verify(analysisDispatchService, times(1)).dispatch(
                conversation, userMessage, ANSWER, ASSISTANT_MESSAGE_ID, "token", opening);
        assertThat(completedRun().getStatus()).isEqualTo(AgentRunStatus.COMPLETED);
    }

    @Test
    @DisplayName("助手消息持久化后 complete() 抛异常：仍触发分析，且不重复保存")
    void completeFailureAfterPersistenceStillAnalyzes() {
        when(messageService.appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList()))
                .thenReturn(assistantMessage());
        handlerWithDeltaAndComplete();

        TestEmitter emitter = new TestEmitter(false, true);
        chatService = serviceWith(emitter);

        chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token");

        assertThat(emitter.completed).isTrue();
        verify(messageService, times(1)).appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList());
        verify(analysisDispatchService, times(1)).dispatch(
                conversation, userMessage, ANSWER, ASSISTANT_MESSAGE_ID, "token", opening);
        assertThat(completedRun().getStatus()).isEqualTo(AgentRunStatus.COMPLETED);
    }

    @Test
    @DisplayName("截断回答：部分内容落库并标记 truncated，run=TRUNCATED，不触发分析")
    void truncatedStreamPersistsPartialButSkipsAnalysis() {
        when(messageService.appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(true), anyList()))
                .thenReturn(assistantMessage());
        doAnswer(invocation -> {
            AgentStreamHandler handler = invocation.getArgument(1);
            handler.onDelta(ANSWER);
            handler.onComplete(new StreamCompletion("length", true));
            return null;
        }).when(agentGateway).streamReply(any(ChatAgentRequest.class), any(AgentStreamHandler.class));

        TestEmitter emitter = new TestEmitter(false, false);
        chatService = serviceWith(emitter);

        chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token");

        // 已生成的部分仍要保存，用户重进对话时不会看到空洞
        verify(messageService).appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(true), anyList());
        // 不完整回答不能作为完整学习证据
        verifyNoInteractions(analysisDispatchService);
        assertThat(emitter.sendCount).isEqualTo(3); // start / delta / end
        assertThat(emitter.completed).isTrue();
        assertThat(completedRun().getStatus()).isEqualTo(AgentRunStatus.TRUNCATED);
        assertThat(completedRun().getOutputSummary()).contains("truncated=true");
    }

    @Test
    @DisplayName("助手消息落库失败：不启动学习分析")
    void assistantPersistenceFailureDoesNotAnalyze() {
        when(messageService.appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList()))
                .thenThrow(new RuntimeException("database unavailable"));
        handlerWithDeltaAndComplete();

        TestEmitter emitter = new TestEmitter(false, false);
        chatService = serviceWith(emitter);

        chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token");

        verifyNoInteractions(analysisDispatchService);
        assertThat(completedRun().getStatus()).isEqualTo(AgentRunStatus.FAILED);
        assertThat(emitter.completed).isTrue();
    }

    @Test
    @DisplayName("Agent 返回结构化引用：随助手消息落库，并出现在 message.end")
    @SuppressWarnings("unchecked")
    void structuredCitationsArePersistedAndSentInEndEvent() {
        when(messageService.appendAssistantMessage(eq(CONVERSATION_ID), eq(ANSWER), eq(false), anyList()))
                .thenReturn(assistantMessage());
        AgentCitation citation = new AgentCitation(10L, 1L, 0, "zenframe.md");
        doAnswer(invocation -> {
            AgentStreamHandler handler = invocation.getArgument(1);
            handler.onDelta(ANSWER);
            handler.onComplete(new StreamCompletion(null, false, List.of(citation)));
            return null;
        }).when(agentGateway).streamReply(any(ChatAgentRequest.class), any(AgentStreamHandler.class));

        TestEmitter emitter = new TestEmitter(false, false);
        chatService = serviceWith(emitter);

        chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token");

        // ① 引用交给持久化层（落库后 GET /messages 才能恢复）
        ArgumentCaptor<List<CitationMetadata>> stored = ArgumentCaptor.forClass(List.class);
        verify(messageService).appendAssistantMessage(
                eq(CONVERSATION_ID), eq(ANSWER), eq(false), stored.capture());
        assertThat(stored.getValue())
                .containsExactly(new CitationMetadata(10L, 1L, 0, "zenframe.md"));

        // ② 同一份引用出现在 message.end，实时链路不需要另查一次
        Map<String, Object> end = emitter.payloadOf("message.end");
        assertThat(end.get("citations"))
                .isEqualTo(List.of(new CitationResponse(10L, 1L, 0, "zenframe.md", true)));
    }

    @Test
    @DisplayName("幂等重放：已完成轮次直接回放原 id，不创建新 run、不调用 Agent")
    void completedReplayReturnsOriginalIdsWithoutRegenerating() {
        Message assistant = assistantMessage();
        runningRun.setStatus(AgentRunStatus.COMPLETED);
        runningRun.setAssistantMessageId(ASSISTANT_MESSAGE_ID);
        when(chatTurnService.findExisting(CONVERSATION_ID, CLIENT_REQUEST_ID))
                .thenReturn(Optional.of(new ChatTurnService.TurnSnapshot(
                        userMessage, null, runningRun, assistant)));

        chatService = serviceWith(new TestEmitter(false, false));

        Throwable thrown = catchThrowable(() ->
                chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token"));

        assertThat(thrown).isInstanceOf(ChatReplayException.class);
        ChatReplayException replay = (ChatReplayException) thrown;
        assertThat(replay.getStatus()).isEqualTo(200);
        assertThat(replay.getResponse().idempotentReplay()).isTrue();
        assertThat(replay.getResponse().state()).isEqualTo(ChatReplayResponse.STATE_COMPLETED);
        assertThat(replay.getResponse().userMessageId()).isEqualTo(USER_MESSAGE_ID);
        assertThat(replay.getResponse().assistantMessageId()).isEqualTo(ASSISTANT_MESSAGE_ID);
        assertThat(replay.getResponse().runId()).isEqualTo(RUN_ID);

        // 重放不生成、不写库：这是幂等的核心要求
        verifyNoInteractions(agentGateway);
        verify(chatTurnService, never()).prepare(any(), any(), any());
    }

    @Test
    @DisplayName("幂等重放：run 仍在 RUNNING 时返回 202 处理中，而不是 200")
    void processingReplayReturns202() {
        when(chatTurnService.findExisting(CONVERSATION_ID, CLIENT_REQUEST_ID))
                .thenReturn(Optional.of(new ChatTurnService.TurnSnapshot(
                        userMessage, null, runningRun, null)));

        chatService = serviceWith(new TestEmitter(false, false));

        Throwable thrown = catchThrowable(() ->
                chatService.stream(USER_ID, CONVERSATION_ID, QUESTION, CLIENT_REQUEST_ID, "token"));

        assertThat(thrown).isInstanceOf(ChatReplayException.class);
        ChatReplayException replay = (ChatReplayException) thrown;
        assertThat(replay.getStatus()).isEqualTo(202);
        assertThat(replay.getResponse().state()).isEqualTo(ChatReplayResponse.STATE_PROCESSING);
        verifyNoInteractions(agentGateway);
        verify(chatTurnService, never()).prepare(any(), any(), any());
    }

    private void handlerWithDeltaAndComplete() {
        doAnswer(invocation -> {
            AgentStreamHandler handler = invocation.getArgument(1);
            handler.onDelta(ANSWER);
            handler.onComplete();
            return null;
        }).when(agentGateway).streamReply(any(ChatAgentRequest.class), any(AgentStreamHandler.class));
    }

    private ChatService serviceWith(SseEmitter emitter) {
        TaskExecutor inline = Runnable::run;
        return new ChatService(conversationService, messageService, agentGateway,
                agentRunMapper, chatTurnService, analysisDispatchService, inline) {
            @Override
            protected SseEmitter createEmitter() {
                return emitter;
            }
        };
    }

    private static Message assistantMessage() {
        Message message = new Message();
        message.setId(ASSISTANT_MESSAGE_ID);
        message.setConversationId(CONVERSATION_ID);
        message.setContent(ANSWER);
        return message;
    }

    private AgentRun completedRun() {
        ArgumentCaptor<AgentRun> captor = ArgumentCaptor.forClass(AgentRun.class);
        verify(agentRunMapper).updateById(captor.capture());
        return captor.getValue();
    }

    /** 可模拟客户端断开时 send / complete 抛异常的最小 SseEmitter。 */
    private static final class TestEmitter extends SseEmitter {

        private final boolean failOnSecondSend;
        private final boolean failComplete;
        private final List<SseEventBuilder> events = new ArrayList<>();
        private int sendCount;
        private boolean completed;

        private TestEmitter(boolean failOnSecondSend, boolean failComplete) {
            this.failOnSecondSend = failOnSecondSend;
            this.failComplete = failComplete;
        }

        @Override
        public void send(SseEventBuilder builder) {
            events.add(builder);
            sendCount++;
            if (failOnSecondSend && sendCount == 2) {
                throw new NullPointerException("client disconnected during send");
            }
        }

        /** 取出指定事件的 data 载荷（未序列化，仍是 send 时传入的对象）。 */
        private Map<String, Object> payloadOf(String eventName) {
            for (SseEventBuilder builder : events) {
                Set<ResponseBodyEmitter.DataWithMediaType> parts = builder.build();
                boolean matches = parts.stream().anyMatch(part ->
                        part.getData() instanceof String text
                                && text.contains("event:" + eventName));
                if (!matches) {
                    continue;
                }
                for (ResponseBodyEmitter.DataWithMediaType part : parts) {
                    if (part.getData() instanceof Map<?, ?> payload) {
                        @SuppressWarnings("unchecked")
                        Map<String, Object> typed = (Map<String, Object>) payload;
                        return typed;
                    }
                }
            }
            return Map.of();
        }

        @Override
        public void complete() {
            completed = true;
            if (failComplete) {
                throw new NullPointerException("client disconnected during complete");
            }
        }
    }
}
