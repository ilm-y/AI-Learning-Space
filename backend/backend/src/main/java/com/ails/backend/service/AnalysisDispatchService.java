package com.ails.backend.service;

import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.mq.AnalysisTaskService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

/**
 * 一轮对话结束后，决定这次学习分析走哪条路。
 * <p>
 * <pre>
 *   ails.analysis.mode = mq      → 建 PENDING 任务并投递 RabbitMQ；投递失败退回同步分析
 *   ails.analysis.mode = inline  → 原来的 TurnAnalysisService（同步调 Agent，失败退 rules-v1）
 * </pre>
 * 只在这里做一次分支，{@link ChatService} 不需要知道 RabbitMQ 的存在。
 * <p>
 * 为什么投递失败要退回同步分析：任务消息没进队列，就意味着这一轮的学习闭环彻底停了。
 * 同步分析虽然慢，但至少能把 Evidence 写进去；代价只是这一轮多占几秒线程。
 */
@Slf4j
@Service
public class AnalysisDispatchService {

    private final TurnAnalysisService turnAnalysisService;
    private final ObjectProvider<AnalysisTaskService> analysisTaskService;
    private final String mode;

    public AnalysisDispatchService(TurnAnalysisService turnAnalysisService,
                                   ObjectProvider<AnalysisTaskService> analysisTaskService,
                                   @Value("${ails.analysis.mode:inline}") String mode) {
        this.turnAnalysisService = turnAnalysisService;
        this.analysisTaskService = analysisTaskService;
        this.mode = mode;
    }

    /**
     * 分析一轮已经完成的对话。异常由调用方（ChatService）吞掉，绝不能影响已经推完的回复。
     */
    public void dispatch(Conversation conversation, Message userMessage,
                         String assistantContent, Long assistantMessageId,
                         String authToken, LearningAnalysisService.TurnOpening opening) {
        if (isMqMode()) {
            AnalysisTaskService taskService = analysisTaskService.getIfAvailable();
            if (taskService != null) {
                AnalysisTaskService.DispatchOutcome outcome = taskService.enqueue(
                        conversation, userMessage, assistantContent, assistantMessageId, opening);
                if (outcome != AnalysisTaskService.DispatchOutcome.FAILED) {
                    if (outcome == AnalysisTaskService.DispatchOutcome.OUTCOME_UNKNOWN) {
                        log.warn("MQ 分析任务投递结果未知，保留 PENDING、不重复分析, messageId={}",
                                userMessage == null ? null : userMessage.getId());
                    }
                    return;
                }
            }
            log.warn("MQ 分析任务确定未投递成功，退回同步分析, messageId={}",
                    userMessage == null ? null : userMessage.getId());
        }
        turnAnalysisService.analyzeTurn(conversation, userMessage, assistantContent,
                assistantMessageId, authToken, opening);
    }

    private boolean isMqMode() {
        return "mq".equalsIgnoreCase(mode == null ? "" : mode.trim());
    }
}
