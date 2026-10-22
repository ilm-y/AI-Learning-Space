package com.ails.backend.mq;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.MessageMapper;
import com.ails.backend.service.AgentAnalysisWriteService;
import com.ails.backend.service.LearningAnalysisService;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.Objects;

/**
 * 结果消息的业务处理：校验 → 抢占任务 → 重新加载上下文 → 写入 → 收尾状态。
 * <p>
 * 这个类刻意与队列监听器分开：监听器只负责 ACK/NACK，业务判断全部在这里，
 * 因此可以在不启动 RabbitMQ 的情况下用普通单测覆盖。
 *
 * <h3>为什么每次都从数据库重建上下文</h3>
 * 结果消息只带回 taskId / traceId / messageId，不携带 userId、spaceId 或用户 JWT。
 * 写库需要的身份与作用域全部从 {@code agent_run} 和 {@code conversation} 重新读取，
 * Worker 无法通过伪造结果消息把证据写到别人的空间里。
 *
 * <h3>幂等</h3>
 * 同一条结果可能被投递多次（消费者重启、网络重连）。处理前先检查 run 是否已终态；
 * 写入本身还有 Evidence dedupe_key 与 Understanding「内容没变不写 Revision」两层保护。
 */
@Slf4j
@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
public class AnalysisResultProcessor {

    private static final int ERROR_MAX_LENGTH = 1000;

    private final AgentRunMapper agentRunMapper;
    private final ConversationMapper conversationMapper;
    private final MessageMapper messageMapper;
    private final AgentAnalysisWriteService writeService;
    private final LearningAnalysisService learningAnalysisService;

    public enum Outcome {
        /** 结果已写入，run 置 SUCCEEDED。 */
        APPLIED,
        /** 任务已经终态，重复消息被忽略。 */
        DUPLICATE,
        /** 消息无法对应到任务（run 不存在 / 类型不对），已 ACK。 */
        IGNORED,
        /** 任务被判定失败，run 置 FAILED。调用方可以继续做 rules-v1 兜底。 */
        FAILED
    }

    /**
     * 处理一条结果消息。整个方法在一个事务里：抢占、写入、收尾要么全成功，要么全回滚。
     * <p>
     * 抛出异常表示「暂时处理不了」（例如数据库连接闪断），调用方应 requeue；
     * 返回 {@link Outcome#FAILED} 表示业务上已经失败，消息可以 ACK。
     */
    @Transactional
    public Outcome process(AnalysisResultMessage message) {
        if (message == null || message.taskId() == null) {
            log.warn("忽略缺少 taskId 的分析结果消息");
            return Outcome.IGNORED;
        }

        // 条件更新先抢占：PENDING 与 STALE 都能变成 RUNNING。STALE 只是补偿扫描
        // 标记的「可能遗留」，任务本身仍可能在队列里/正在分析，晚到的合法结果必须能收敛。
        // 返回 0 表示任务已被别人处理过，或者已经是终态。用 FOR UPDATE 重新读一次拿到
        // 最新状态（REPEATABLE_READ 下普通 SELECT 可能还停在旧快照）。
        int claimed = agentRunMapper.update(null, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, message.taskId())
                .in(AgentRun::getStatus, AgentRunStatus.PENDING, AgentRunStatus.STALE)
                .set(AgentRun::getStatus, AgentRunStatus.RUNNING));
        AgentRun run = agentRunMapper.selectOne(new LambdaQueryWrapper<AgentRun>()
                .eq(AgentRun::getId, message.taskId())
                .last("FOR UPDATE"));

        if (run == null) {
            log.warn("忽略分析结果：任务不存在, taskId={}", message.taskId());
            return Outcome.IGNORED;
        }
        if (run.getRunType() != AgentRunType.ANALYSIS) {
            log.warn("忽略分析结果：任务类型不是 ANALYSIS, taskId={}, runType={}",
                    message.taskId(), run.getRunType());
            return Outcome.IGNORED;
        }
        if (claimed == 0 && isTerminal(run.getStatus())) {
            log.info("分析结果重复投递，已忽略, taskId={}, status={}", message.taskId(), run.getStatus());
            return Outcome.DUPLICATE;
        }
        // claimed == 0 且状态仍是 RUNNING：上一次消费者处理到一半崩溃，消息被重新投递。
        // 继续走写入；Evidence 去重与 Revision 的「内容不变不落版本」保证不会重复记账。

        if (message.schemaVersion() != AnalysisResultMessage.SCHEMA_VERSION) {
            return fail(run, "不支持的结果 schemaVersion=" + message.schemaVersion());
        }
        if (!Objects.equals(run.getTraceId(), message.traceId())) {
            return fail(run, "结果消息 traceId 与任务不一致：task=" + run.getTraceId()
                    + ", message=" + message.traceId());
        }
        if (message.status() == null) {
            return fail(run, "结果消息缺少 status");
        }
        if (message.status() == AnalysisResultMessage.Status.FAILED) {
            return fail(run, describeFailure(message));
        }
        if (message.result() == null) {
            return fail(run, "status=SUCCEEDED 但缺少 result");
        }

        Conversation conversation = conversationMapper.selectById(run.getConversationId());
        Message userMessage = message.messageId() == null
                ? null : messageMapper.selectById(message.messageId());
        String contextError = validateContext(run, conversation, userMessage);
        if (contextError != null) {
            return fail(run, contextError);
        }

        AgentAnalysisWriteService.TurnContext context = new AgentAnalysisWriteService.TurnContext(
                run.getUserId(),
                conversation.getId(),
                conversation.getLearningSpaceId(),
                userMessage.getId(),
                run.getId());
        AgentAnalysisWriteService.WriteResult written = writeService.apply(context, message.result());
        markSucceeded(run, written.summary());
        log.info("分析结果已写入, taskId={}, traceId={}, {}", run.getId(), run.getTraceId(), written.summary());
        return Outcome.APPLIED;
    }

    /**
     * 结果消息重试耗尽后的最后一步：把仍停在 PENDING / RUNNING 的任务落成 FAILED。
     * <p>
     * 这是一次「尽力而为」的更新 —— 重试耗尽往往正是因为数据库不可用，所以调用方必须
     * 容忍这里继续失败。此时消息已经进死信，人工补偿仍能按 taskId 找到它。
     * 不复用 {@link #fail} 是因为不需要读 run，也拿不到可靠的 createdAt（duration 留空）。
     */
    @Transactional
    public void markFailedPermanently(Long taskId, String error) {
        if (taskId == null) {
            return;
        }
        AgentRun patch = new AgentRun();
        patch.setStatus(AgentRunStatus.FAILED);
        patch.setErrorMessage(truncate(error, ERROR_MAX_LENGTH));
        patch.setFinishedAt(LocalDateTime.now());
        agentRunMapper.update(patch, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, taskId)
                .in(AgentRun::getStatus,
                        AgentRunStatus.PENDING, AgentRunStatus.STALE, AgentRunStatus.RUNNING));
    }

    /**
     * 分析失败后的 rules-v1 兜底。单独一个事务：兜底失败绝不能回滚已经落下的 FAILED 状态。
     * <p>
     * 兜底会自己建一条 ANALYSIS run（{@code LearningAnalysisService} 的既有行为），
     * 所以失败任务在轨迹上会看到两条记录：FAILED 的 Agent 任务 + COMPLETED 的规则任务。
     * 这是刻意保留的语义 —— 前者说明 Agent 这一跳失败，后者说明学习闭环仍然往前走了一步。
     */
    @Transactional
    public void fallbackIfNeeded(AnalysisResultMessage message) {
        if (message == null || message.messageId() == null) {
            return;
        }
        try {
            Message userMessage = messageMapper.selectById(message.messageId());
            if (userMessage == null) {
                log.warn("跳过 rules-v1 兜底：来源消息不存在, messageId={}", message.messageId());
                return;
            }
            learningAnalysisService.analyzeMessage(userMessage);
        } catch (Exception e) {
            // 兜底本身失败不影响结果消息的 ACK：失败已经记录，重复执行也不会更好
            log.error("rules-v1 兜底失败, taskId={}, messageId={}",
                    message.taskId(), message.messageId(), e);
        }
    }

    // ================= 内部 =================

    private static boolean isTerminal(AgentRunStatus status) {
        return status == AgentRunStatus.SUCCEEDED
                || status == AgentRunStatus.FAILED
                || status == AgentRunStatus.COMPLETED;
    }

    private String validateContext(AgentRun run, Conversation conversation, Message userMessage) {
        if (conversation == null) {
            return "分析上下文缺失：conversation 不存在";
        }
        if (!Objects.equals(conversation.getUserId(), run.getUserId())) {
            return "分析上下文不一致：conversation.userId 与任务归属不符";
        }
        if (userMessage == null) {
            return "分析上下文缺失：来源消息不存在";
        }
        if (!Objects.equals(userMessage.getConversationId(), conversation.getId())) {
            return "分析上下文不一致：来源消息不属于该对话";
        }
        return null;
    }

    private Outcome fail(AgentRun run, String error) {
        LocalDateTime now = LocalDateTime.now();
        AgentRun patch = new AgentRun();
        patch.setStatus(AgentRunStatus.FAILED);
        patch.setErrorMessage(truncate(error, ERROR_MAX_LENGTH));
        patch.setFinishedAt(now);
        patch.setDurationMs(toMillis(run.getCreatedAt(), now));
        agentRunMapper.update(patch, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, run.getId())
                .in(AgentRun::getStatus,
                        AgentRunStatus.PENDING, AgentRunStatus.STALE, AgentRunStatus.RUNNING));
        log.warn("分析任务失败, taskId={}, traceId={}: {}", run.getId(), run.getTraceId(), error);
        return Outcome.FAILED;
    }

    private void markSucceeded(AgentRun run, String outputSummary) {
        LocalDateTime now = LocalDateTime.now();
        AgentRun patch = new AgentRun();
        patch.setStatus(AgentRunStatus.SUCCEEDED);
        patch.setOutputSummary(outputSummary);
        patch.setFinishedAt(now);
        patch.setDurationMs(toMillis(run.getCreatedAt(), now));
        agentRunMapper.update(patch, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, run.getId())
                .eq(AgentRun::getStatus, AgentRunStatus.RUNNING));
    }

    private static String describeFailure(AnalysisResultMessage message) {
        String code = message.errorCode() == null ? "UNKNOWN" : message.errorCode();
        String detail = message.errorMessage() == null ? "" : message.errorMessage();
        return "Agent 分析失败[" + code + "] " + detail;
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
