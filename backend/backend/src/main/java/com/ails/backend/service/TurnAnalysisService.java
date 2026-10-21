package com.ails.backend.service;

import com.ails.backend.agent.AgentAnalysisGateway;
import com.ails.backend.dto.agent.AnalyzeAgentRequest;
import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.LearningSession;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.utils.TraceIds;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

/**
 * 一轮对话结束后的学习分析编排。
 * <p>
 * 两条路径，一个出口：
 * <pre>
 *   Agent 可用（ails.agent.gateway=http）：
 *       建 ANALYSIS run → POST /agent/analyze → 在事务里写入结果
 *   失败 / 未启用（stub）：
 *       退回本地 rules-v1（{@link LearningAnalysisService#analyzeMessage}）
 * </pre>
 * 退回是<b>刻意的</b>：分析失败不影响用户已经拿到的回复，也不影响下一轮对话。
 * 区别只体现在证据的精细程度上 —— rules-v1 只能识别「消息里提到了哪个概念」，
 * Agent 才能判断对错、误区与验证时机。失败痕迹留在 {@code agent_run.error_message}。
 *
 * <p><b>为什么本类不是 {@code @Transactional}</b>：中间有一次最长 90 秒的 HTTP 调用。
 * 把它包进事务等于让一条数据库连接陪着等模型，而且失败后还要在同一事务里退回规则版
 * （那会直接撞上 rollback-only）。事务边界收在 {@link AgentAnalysisWriteService#apply}，
 * 规则版自己也是事务方法 —— 两段各自完整，互不牵连。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class TurnAnalysisService {

    /** 送给 Agent 的历史条数，与 Chat 那一跳保持一致。 */
    private static final int HISTORY_LIMIT = 20;
    /** agent_run.input_summary / error_message 的截断长度。 */
    private static final int SUMMARY_MAX_LENGTH = 255;
    private static final int ERROR_MAX_LENGTH = 1000;

    private final AgentAnalysisGateway agentAnalysisGateway;
    private final AgentAnalysisWriteService writeService;
    private final LearningAnalysisService learningAnalysisService;
    private final MessageService messageService;
    private final AgentRunMapper agentRunMapper;

    /**
     * 分析一轮对话。
     * <p>
     * 调用方（{@link ChatService}）已经完成回复落库；本方法只读用户/助手内容，不再改消息表
     * （唯一的例外是 Agent 决定出题时插入的 TEST 消息，见
     * {@link VerificationService}）。
     *
     * @param assistantContent 助手回复全文，可空（流式生成失败时仍分析用户消息）
     * @param assistantMessageId 已落库的助手消息 id，可空；只用于从历史里排除本轮
     * @param opening 本轮开始时的 Concept 匹配与 Session 归属；只用于规则版与 Session 上下文
     * @return 本次真正新增的 Evidence（命中去重的不算）；分析被跳过时返回空列表
     */
    public List<LearningEvidence> analyzeTurn(Conversation conversation, Message userMessage,
                                              String assistantContent, Long assistantMessageId,
                                              String authToken,
                                              LearningAnalysisService.TurnOpening opening) {
        if (conversation == null || userMessage == null) {
            return List.of();
        }
        List<Concept> matched = opening == null ? List.of() : opening.matched();

        if (!agentAnalysisGateway.enabled()) {
            // 默认的 stub 模式：不建空的 ANALYSIS run，直接走本地规则版，
            // 这样「没接 Agent」的部署里 agent_run 表不会每轮多一条无意义记录。
            return learningAnalysisService.analyzeMessage(userMessage, matched);
        }

        LocalDateTime startedAt = LocalDateTime.now();
        AgentRun run = createRun(conversation, userMessage);
        AgentAnalysisWriteService.TurnContext context = new AgentAnalysisWriteService.TurnContext(
                conversation.getUserId(),
                conversation.getId(),
                conversation.getLearningSpaceId(),
                userMessage.getId(),
                run.getId());

        AgentAnalysisWriteService.WriteResult written;
        try {
            AnalyzeAgentResult result = agentAnalysisGateway.analyze(buildRequest(
                    run, conversation, userMessage, assistantContent, assistantMessageId, authToken, opening));
            written = writeService.apply(context, result);
        } catch (RuntimeException e) {
            // Agent 不可用、返回不合契约、或写入事务回滚 —— 三种情况都退回规则版。
            // 注意这里捕获的是已经离开 @Transactional 方法的异常，事务已完整回滚，
            // 不会把「半份分析」留在库里。
            log.warn("agent 分析失败，退回 rules-v1, userId={}, messageId={}, runId={}: {}",
                    conversation.getUserId(), userMessage.getId(), run.getId(), e.toString());
            finishRun(run, AgentRunStatus.FAILED, null, e.toString(), startedAt);
            return learningAnalysisService.analyzeMessage(userMessage, matched);
        }

        // 收尾放在 try 之外：分析已经成功落库，这一步失败不该再触发一次规则版分析，
        // 否则同一轮会产生两套 Evidence。
        finishRun(run, AgentRunStatus.COMPLETED, written.summary(), null, startedAt);
        return written.evidences();
    }

    private AnalyzeAgentRequest buildRequest(AgentRun run, Conversation conversation,
                                             Message userMessage, String assistantContent,
                                             Long assistantMessageId, String authToken,
                                             LearningAnalysisService.TurnOpening opening) {
        // 本轮的用户消息与助手消息都已经在表里，历史必须把两者都排除：
        // 它们已经通过 userMessage / assistantMessage 字段单独传给了 Agent。
        List<Long> excluded = new ArrayList<>(2);
        excluded.add(userMessage.getId());
        if (assistantMessageId != null) {
            excluded.add(assistantMessageId);
        }
        return new AnalyzeAgentRequest(
                run.getId(),
                run.getTraceId(),
                conversation.getUserId(),
                conversation.getId(),
                conversation.getLearningSpaceId(),
                userMessage.getId(),
                userMessage.getContent(),
                assistantContent,
                messageService.history(conversation.getId(), excluded, HISTORY_LIMIT),
                sessionContext(opening),
                authToken);
    }

    // ================= agent_run =================

    private AgentRun createRun(Conversation conversation, Message userMessage) {
        AgentRun run = new AgentRun();
        run.setUserId(conversation.getUserId());
        run.setConversationId(conversation.getId());
        // 与 CHAT run 同源：beginTurn 已经把 Session 归属写回 conversation，
        // 这里直接照抄，避免两处对同一条对话给出不同的学习活动。
        run.setSessionId(conversation.getSessionId());
        run.setRunType(AgentRunType.ANALYSIS);
        run.setStatus(AgentRunStatus.RUNNING);
        run.setInputSummary(truncate(userMessage.getContent(), SUMMARY_MAX_LENGTH));
        run.setTraceId(TraceIds.newId());
        agentRunMapper.insert(run);
        return run;
    }

    /**
     * 与 ChatService 收尾 run 的写法一致：只带 id 的空实体，null 字段不会被写回。
     * 这里的 run 一定还是 RUNNING（分析路径没有第二个收尾者），所以无需条件更新。
     */
    private void finishRun(AgentRun run, AgentRunStatus status, String outputSummary,
                           String error, LocalDateTime startedAt) {
        AgentRun patch = new AgentRun();
        patch.setId(run.getId());
        patch.setStatus(status);
        patch.setOutputSummary(outputSummary);
        patch.setErrorMessage(truncate(error, ERROR_MAX_LENGTH));
        patch.setFinishedAt(LocalDateTime.now());
        patch.setDurationMs(toMillis(startedAt, patch.getFinishedAt()));
        agentRunMapper.updateById(patch);
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
        return (int) Math.min(Integer.MAX_VALUE, Duration.between(from, to).toMillis());
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
