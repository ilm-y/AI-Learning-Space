package com.ails.backend.service;

import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.mq.AnalysisTaskService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.beans.factory.ObjectProvider;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 「MQ 优先、有条件退回同步分析」这条分支的单测。
 * <p>
 * 只有<b>确定失败</b>才退回 inline；结果未知（确认超时等）必须保留 PENDING，
 * 否则消息已经在队列里却又跑一遍同步分析，同一个任务会调用两次模型。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisDispatchServiceTest {

    @Mock
    private TurnAnalysisService turnAnalysisService;
    @Mock
    private ObjectProvider<AnalysisTaskService> taskServiceProvider;
    @Mock
    private AnalysisTaskService taskService;

    private final Conversation conversation = new Conversation();
    private final Message userMessage = new Message();

    @Test
    void mqModePublishesAndDoesNotRunInlineAnalysis() {
        when(taskServiceProvider.getIfAvailable()).thenReturn(taskService);
        when(taskService.enqueue(any(), any(), any(), any(), any()))
                .thenReturn(AnalysisTaskService.DispatchOutcome.CONFIRMED);
        AnalysisDispatchService service = new AnalysisDispatchService(
                turnAnalysisService, taskServiceProvider, "mq");

        service.dispatch(conversation, userMessage, "assistant", 1L, "jwt", null);

        verify(taskService).enqueue(conversation, userMessage, "assistant", 1L, null);
        verify(turnAnalysisService, never()).analyzeTurn(any(), any(), any(), any(), anyString(), any());
    }

    @Test
    void unknownOutcomeDoesNotTriggerInlineAnalysis() {
        when(taskServiceProvider.getIfAvailable()).thenReturn(taskService);
        when(taskService.enqueue(any(), any(), any(), any(), any()))
                .thenReturn(AnalysisTaskService.DispatchOutcome.OUTCOME_UNKNOWN);
        AnalysisDispatchService service = new AnalysisDispatchService(
                turnAnalysisService, taskServiceProvider, "mq");

        service.dispatch(conversation, userMessage, "assistant", 1L, "jwt", null);

        // 消息可能已入队：再跑 inline 就是第二次模型调用
        verify(turnAnalysisService, never()).analyzeTurn(any(), any(), any(), any(), anyString(), any());
    }

    @Test
    void publishFailureFallsBackToInlineAnalysis() {
        when(taskServiceProvider.getIfAvailable()).thenReturn(taskService);
        when(taskService.enqueue(any(), any(), any(), any(), any()))
                .thenReturn(AnalysisTaskService.DispatchOutcome.FAILED);
        AnalysisDispatchService service = new AnalysisDispatchService(
                turnAnalysisService, taskServiceProvider, "mq");

        service.dispatch(conversation, userMessage, "assistant", 1L, "jwt", null);

        verify(turnAnalysisService).analyzeTurn(conversation, userMessage, "assistant", 1L, "jwt", null);
    }

    @Test
    void inlineModeNeverTouchesTheQueue() {
        AnalysisDispatchService service = new AnalysisDispatchService(
                turnAnalysisService, taskServiceProvider, "inline");

        service.dispatch(conversation, userMessage, "assistant", 1L, "jwt", null);

        verify(taskServiceProvider, never()).getIfAvailable();
        verify(turnAnalysisService).analyzeTurn(conversation, userMessage, "assistant", 1L, "jwt", null);
    }
}
