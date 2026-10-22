package com.ails.backend.mq;

import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.LearningSession;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.service.LearningAnalysisService;
import com.ails.backend.service.MessageService;
import com.ails.backend.utils.TraceIds;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

/**
 * 创建异步分析任务并投递到 RabbitMQ。
 * <p>
 * 与 {@link com.ails.backend.service.TurnAnalysisService} 的分工：
 * <ul>
 *   <li>本类只负责「建任务 + 投递」，不调用任何模型；</li>
 *   <li>分析由 Python Worker 执行；</li>
 *   <li>写入仍由 {@link com.ails.backend.service.AgentAnalysisWriteService} 完成。</li>
 * </ul>
 *
 * <h3>事务与投递不是原子的</h3>
 * 先 {@code INSERT agent_run(PENDING)} 再投递。两者之间进程崩溃会留下一条永远 PENDING 的任务，
 * 这是第一版明确接受的窗口：投递失败会立刻标 FAILED 并退回同步分析；进程崩溃留下的 PENDING
 * 可以靠「按 created_at 扫描 PENDING 超过阈值」的补偿任务发现（不在本类里自动做）。
 * 进一步提高可靠性时应换成 Transactional Outbox，而不是把 MQ 发布塞进数据库事务。
 */
@Slf4j
@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
public class AnalysisTaskService {

    /** 与 Chat / TurnAnalysis 保持一致：分析只看最近 20 条历史。 */
    private static final int HISTORY_LIMIT = 20;
    private static final int SUMMARY_MAX_LENGTH = 255;
    private static final int ERROR_MAX_LENGTH = 1000;

    private final AgentRunMapper agentRunMapper;
    private final MessageService messageService;
    private final AnalysisTaskPublisher publisher;

    /** 投递结果。未知结果必须与确定失败分开，否则会重复调用模型。 */
    public enum DispatchOutcome {
        /** broker 已确认接收且消息可路由。 */
        CONFIRMED,
        /** 等确认超时/中断：消息可能已在队列中，也可能没有。 */
        OUTCOME_UNKNOWN,
        /** 确定没有投出去，调用方应退回同步分析。 */
        FAILED
    }

    /**
     * 创建并投递一次分析任务。
     *
     * @return {@link DispatchOutcome#CONFIRMED} 表示不需要同步兜底；
     *         {@link DispatchOutcome#OUTCOME_UNKNOWN} 表示结果未知，保留 PENDING、不重复分析；
     *         {@link DispatchOutcome#FAILED} 表示确定投递失败，调用方应退回同步分析
     */
    public DispatchOutcome enqueue(Conversation conversation, Message userMessage,
                                   String assistantContent, Long assistantMessageId,
                                   LearningAnalysisService.TurnOpening opening) {
        if (conversation == null || userMessage == null) {
            return DispatchOutcome.FAILED;
        }
        AgentRun run = createRun(conversation, userMessage);
        try {
            publisher.publish(buildMessage(
                    run, conversation, userMessage, assistantContent, assistantMessageId, opening));
            return DispatchOutcome.CONFIRMED;
        } catch (AnalysisPublishException e) {
            if (e.outcome() == AnalysisPublishException.Outcome.UNKNOWN) {
                // 消息可能已经被 Worker 消费。此时既不能把它标成 FAILED，
                // 也不能立刻跑同步分析 —— 那会让同一个任务调用两次模型。
                // 保留 PENDING，记录可排查的原因；晚到的结果仍然可以正常收敛。
                log.warn("分析任务投递结果未知，保留 PENDING 等待结果, runId={}, messageId={}: {}",
                        run.getId(), userMessage.getId(), e.toString());
                markDispatchUnknown(run, e.toString());
                return DispatchOutcome.OUTCOME_UNKNOWN;
            }
            log.error("分析任务投递失败, runId={}, messageId={}: {}",
                    run.getId(), userMessage.getId(), e.toString());
            return markFailed(run, e.toString())
                    ? DispatchOutcome.CONFIRMED : DispatchOutcome.FAILED;
        } catch (RuntimeException e) {
            // publisher 之外的意外异常（例如本地构造消息失败）发生在发送之前，
            // 仍然按确定失败处理，保留原有的同步兜底能力。
            log.error("分析任务投递失败, runId={}, messageId={}: {}",
                    run.getId(), userMessage.getId(), e.toString());
            return markFailed(run, e.toString())
                    ? DispatchOutcome.CONFIRMED : DispatchOutcome.FAILED;
        }
    }

    private AgentRun createRun(Conversation conversation, Message userMessage) {
        AgentRun run = new AgentRun();
        run.setUserId(conversation.getUserId());
        run.setConversationId(conversation.getId());
        // 与 TurnAnalysisService / LearningAnalysisService 一致：分析 run 的归属照抄对话。
        run.setSessionId(conversation.getSessionId());
        run.setRunType(AgentRunType.ANALYSIS);
        // PENDING 而不是 RUNNING：任务只是「已交给队列」，还没有任何 Worker 开始处理。
        run.setStatus(AgentRunStatus.PENDING);
        run.setInputSummary(truncate(userMessage.getContent(), SUMMARY_MAX_LENGTH));
        run.setTraceId(TraceIds.newId());
        agentRunMapper.insert(run);
        return run;
    }

    private AnalysisTaskMessage buildMessage(AgentRun run, Conversation conversation, Message userMessage,
                                             String assistantContent, Long assistantMessageId,
                                             LearningAnalysisService.TurnOpening opening) {
        // 本轮的用户消息与助手消息都已经在表里，历史必须把两者都排除：
        // 它们已经通过独立字段传给 Worker，再出现在历史末尾就是同一轮说了两遍。
        List<Long> excluded = new ArrayList<>(2);
        excluded.add(userMessage.getId());
        if (assistantMessageId != null) {
            excluded.add(assistantMessageId);
        }
        return new AnalysisTaskMessage(
                AnalysisTaskMessage.SCHEMA_VERSION,
                run.getId(),
                run.getId(),
                run.getTraceId(),
                conversation.getUserId(),
                conversation.getId(),
                conversation.getLearningSpaceId(),
                conversation.getSessionId(),
                userMessage.getId(),
                assistantMessageId,
                userMessage.getContent(),
                assistantContent,
                messageService.history(conversation.getId(), excluded, HISTORY_LIMIT),
                sessionContext(opening));
    }

    /**
     * 投递结果未知时的收尾：只写诊断信息，<b>不改变状态</b>。
     * <p>
     * 任务继续停在 PENDING，晚到的结果仍能抢占并落库；如果消息确实没进队列，
     * 补偿扫描会把它标记成 {@code STALE}（不是 FAILED），留给人工判断。
     * 这里用条件更新，避免覆盖并发写入的终态。
     */
    private void markDispatchUnknown(AgentRun run, String error) {
        AgentRun patch = new AgentRun();
        patch.setErrorMessage(truncate("投递结果未知（消息可能已入队）: " + error, ERROR_MAX_LENGTH));
        int updated = agentRunMapper.update(patch, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, run.getId())
                .in(AgentRun::getStatus, AgentRunStatus.PENDING, AgentRunStatus.RUNNING));
        if (updated == 0) {
            log.debug("投递结果未知，但任务已离开 PENDING/RUNNING，不再写诊断信息, runId={}", run.getId());
        }
    }

    /**
     * 投递失败后的收尾。
     * <p>
     * <b>不能无条件退回同步分析。</b> 发布确认超时只说明「没等到 ack」，消息可能已经被
     * Worker 消费、结果甚至已经落库；这时条件更新会命中 0 行。若忽略这个 0 继续走
     * inline，同一个任务就被分析了两遍（即使 Evidence dedupe 能挡住完全相同的观察，
     * 模型两次输出的观察文本通常并不相同）。
     *
     * @return true 表示任务已是终态、异步链路已经完成，不要同步兜底；
     *         false 表示本次已把任务标成 FAILED，需要同步兜底
     */
    private boolean markFailed(AgentRun run, String error) {
        LocalDateTime now = LocalDateTime.now();
        AgentRun patch = new AgentRun();
        patch.setStatus(AgentRunStatus.FAILED);
        patch.setErrorMessage(truncate(error, ERROR_MAX_LENGTH));
        patch.setFinishedAt(now);
        patch.setDurationMs(toMillis(run.getCreatedAt(), now));
        int updated = agentRunMapper.update(patch, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, run.getId())
                .in(AgentRun::getStatus, AgentRunStatus.PENDING, AgentRunStatus.RUNNING));
        if (updated > 0) {
            return false;
        }
        AgentRun current = agentRunMapper.selectById(run.getId());
        if (current != null && (current.getStatus() == AgentRunStatus.SUCCEEDED
                || current.getStatus() == AgentRunStatus.COMPLETED)) {
            log.warn("分析任务投递确认失败，但异步链路已完成，跳过同步兜底, runId={}, status={}",
                    run.getId(), current.getStatus());
            return true;
        }
        // 行被删掉，或仍停在 PENDING/RUNNING（例如刚才的更新被并发事务挡住又未命中）：
        // 没有证据表明异步链路会完成，交给同步分析。
        return false;
    }

    private static ChatAgentRequest.SessionContext sessionContext(LearningAnalysisService.TurnOpening opening) {
        LearningSession session = opening == null ? null : opening.session();
        if (session == null) {
            return null;
        }
        return new ChatAgentRequest.SessionContext(
                session.getId(), session.getTitle(), session.getGoal(), session.getCurrentFocus());
    }

    private static int toMillis(LocalDateTime from, LocalDateTime to) {
        if (from == null || to == null) {
            return 0;
        }
        return (int) Math.min(Integer.MAX_VALUE, Duration.between(from, to).toMillis());
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
