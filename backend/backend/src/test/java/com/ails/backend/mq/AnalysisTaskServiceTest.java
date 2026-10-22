package com.ails.backend.mq;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.service.MessageService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 投递结果的三种去向：确认成功、确定失败（可退回同步分析）、结果未知（不能重复分析）。
 * <p>
 * 核心场景：发布确认超时，但消息其实已经被 Worker 消费、结果也已经落库。
 * 这时任务可能仍是 PENDING（结果还没到）也可能已是 SUCCEEDED。无论哪种，
 * 都不应该立刻跑同步分析 —— 那会让同一个任务调用两次模型。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisTaskServiceTest {

    private static final long RUN_ID = 42L;

    @Mock
    private AgentRunMapper agentRunMapper;
    @Mock
    private MessageService messageService;
    @Mock
    private AnalysisTaskPublisher publisher;

    private AnalysisTaskService service;
    private Conversation conversation;
    private Message userMessage;

    @BeforeEach
    void setUp() {
        service = new AnalysisTaskService(agentRunMapper, messageService, publisher);

        conversation = new Conversation();
        conversation.setId(2L);
        conversation.setUserId(1L);
        conversation.setLearningSpaceId(3L);

        userMessage = new Message();
        userMessage.setId(100L);
        userMessage.setContent("什么是闭包？");

        lenient().when(agentRunMapper.insert(any(AgentRun.class))).thenAnswer(invocation -> {
            AgentRun run = invocation.getArgument(0);
            run.setId(RUN_ID);
            return 1;
        });
        lenient().when(messageService.history(eq(2L), anyList(), eq(20))).thenReturn(List.of());
    }

    @Test
    void successfulPublishIsConfirmed() {
        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(conversation, userMessage, "回答", 101L, null);

        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.CONFIRMED);
        verify(publisher).publish(any(AnalysisTaskMessage.class));
        verify(agentRunMapper, never()).update(any(AgentRun.class), any());
    }

    @Test
    void definitePublishFailureMarksRunFailedAndFallsBack() {
        doThrow(AnalysisPublishException.definite("连接 RabbitMQ 失败", null))
                .when(publisher).publish(any());
        when(agentRunMapper.update(any(AgentRun.class), any())).thenReturn(1);

        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(conversation, userMessage, "回答", 101L, null);

        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.FAILED);
        ArgumentCaptor<AgentRun> patch = ArgumentCaptor.forClass(AgentRun.class);
        verify(agentRunMapper).update(patch.capture(), any());
        assertThat(patch.getValue().getStatus()).isEqualTo(AgentRunStatus.FAILED);
        assertThat(patch.getValue().getErrorMessage()).contains("连接 RabbitMQ 失败");
        verify(agentRunMapper, never()).selectById(any());
    }

    @Test
    void unknownOutcomeKeepsPendingAndDoesNotFallBack() {
        doThrow(AnalysisPublishException.unknown("等待 RabbitMQ 发布确认失败", null))
                .when(publisher).publish(any());
        when(agentRunMapper.update(any(AgentRun.class), any())).thenReturn(1);

        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(conversation, userMessage, "回答", 101L, null);

        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.OUTCOME_UNKNOWN);
        // 只写诊断信息，状态仍是 PENDING：消息可能已经在队列里，晚到的结果还要收敛。
        ArgumentCaptor<AgentRun> patch = ArgumentCaptor.forClass(AgentRun.class);
        verify(agentRunMapper).update(patch.capture(), any());
        assertThat(patch.getValue().getStatus()).isNull();
        assertThat(patch.getValue().getErrorMessage()).contains("投递结果未知");
        verify(agentRunMapper, never()).selectById(any());
    }

    @Test
    void definiteFailureButAlreadySucceededSkipsFallback() {
        doThrow(AnalysisPublishException.definite("确认超时", null)).when(publisher).publish(any());
        when(agentRunMapper.update(any(AgentRun.class), any())).thenReturn(0);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(runWithStatus(AgentRunStatus.SUCCEEDED));

        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(conversation, userMessage, "回答", 101L, null);

        // 异步链路已经写完结果，绝不能再同步分析一遍
        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.CONFIRMED);
    }

    @Test
    void definiteFailureButCompletedSkipsFallback() {
        doThrow(AnalysisPublishException.definite("确认超时", null)).when(publisher).publish(any());
        when(agentRunMapper.update(any(AgentRun.class), any())).thenReturn(0);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(runWithStatus(AgentRunStatus.COMPLETED));

        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(conversation, userMessage, "回答", 101L, null);

        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.CONFIRMED);
    }

    @Test
    void definiteFailureWhileRunningStillFallsBack() {
        doThrow(AnalysisPublishException.definite("连接失败", null)).when(publisher).publish(any());
        when(agentRunMapper.update(any(AgentRun.class), any())).thenReturn(0);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(runWithStatus(AgentRunStatus.RUNNING));

        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(conversation, userMessage, "回答", 101L, null);

        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.FAILED);
    }

    @Test
    void missingConversationDoesNotCreateRun() {
        AnalysisTaskService.DispatchOutcome outcome =
                service.enqueue(null, userMessage, "回答", 101L, null);

        assertThat(outcome).isEqualTo(AnalysisTaskService.DispatchOutcome.FAILED);
        verify(agentRunMapper, never()).insert(any(AgentRun.class));
    }

    private static AgentRun runWithStatus(AgentRunStatus status) {
        AgentRun run = new AgentRun();
        run.setId(RUN_ID);
        run.setStatus(status);
        return run;
    }
}
