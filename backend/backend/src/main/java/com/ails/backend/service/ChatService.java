package com.ails.backend.service;

import com.ails.backend.agent.AgentGateway;
import com.ails.backend.agent.AgentStreamHandler;
import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.enums.ConversationStatus;
import com.ails.backend.mapper.AgentRunMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.task.TaskExecutor;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 流式对话。
 * <p>
 * 一次请求的事件契约（前端按事件名分发，不要按到达顺序猜）：
 * <pre>
 *   message.start   { runId, conversationId }
 *   message.delta   { delta }
 *   message.end     { messageId, runId }
 *   message.error   { runId, code, message }
 * </pre>
 * runId 从第一个事件起就存在 —— 它是一条 {@code agent_run}（run_type = CHAT）的主键，
 * 状态走 RUNNING → COMPLETED / FAILED。前端拿到 runId 就能去查到这次请求的轨迹。
 * <p>
 * 真实的生成能力由 {@link AgentGateway} 提供，本类不认识 Python，也不认识桩实现。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChatService {

    /** SSE 连接最长存活时间 */
    private static final long SSE_TIMEOUT_MS = 5 * 60 * 1000L;
    /** 送给 Agent 的历史消息条数 */
    private static final int HISTORY_LIMIT = 20;

    private final ConversationService conversationService;
    private final MessageService messageService;
    private final AgentGateway agentGateway;
    private final AgentRunMapper agentRunMapper;
    private final LearningAnalysisService learningAnalysisService;
    /** 见 {@link com.ails.backend.config.AsyncConfig}：自定义 TaskExecutor 会让 Spring Boot
     *  的 applicationTaskExecutor 自动退让，因此这里不会有第二个同类型 Bean 造成歧义 */
    private final TaskExecutor chatStreamExecutor;

    /**
     * 发一条用户消息并流式返回助手回复。
     * <p>
     * 用户消息在请求线程里同步落库（保证 GET /messages 立刻能看到、序号按请求顺序分配），
     * 生成与推送放到独立线程池。
     */
    public SseEmitter stream(Long userId, Long conversationId, String content) {
        Conversation conversation = conversationService.ownedConversation(userId, conversationId);
        if (conversation.getStatus() != ConversationStatus.ACTIVE) {
            throw new BusinessException(400, "对话已归档，无法发送消息");
        }

        Message userMessage = messageService.appendUserMessage(userId, conversationId, content);
        AgentRun run = createRun(userId, conversationId, content);

        SseEmitter emitter = new SseEmitter(SSE_TIMEOUT_MS);
        AtomicBoolean finished = new AtomicBoolean(false);
        LocalDateTime startedAt = LocalDateTime.now();
        emitter.onTimeout(() -> finishRun(run, AgentRunStatus.FAILED, null, "SSE 超时", startedAt));
        emitter.onError(e -> finishRun(run, AgentRunStatus.FAILED, null, String.valueOf(e.getMessage()), startedAt));

        chatStreamExecutor.execute(() ->
                streamReply(emitter, run, conversation, userMessage, finished, startedAt));

        return emitter;
    }

    // ================= 流式推送 =================

    private void streamReply(SseEmitter emitter, AgentRun run, Conversation conversation,
                             Message userMessage, AtomicBoolean finished, LocalDateTime startedAt) {
        StringBuilder buffer = new StringBuilder();

        send(emitter, "message.start", Map.of(
                "runId", run.getId(),
                "conversationId", conversation.getId()));

        try {
            ChatAgentRequest request = new ChatAgentRequest(
                    run.getId(),
                    run.getUserId(),
                    conversation.getId(),
                    conversation.getLearningSpaceId(),
                    userMessage.getContent(),
                    messageService.history(conversation.getId(), HISTORY_LIMIT));

            agentGateway.streamReply(request, new AgentStreamHandler() {
                @Override
                public void onDelta(String delta) {
                    if (delta == null || delta.isEmpty()) {
                        return;
                    }
                    buffer.append(delta);
                    send(emitter, "message.delta", Map.of("delta", delta));
                }

                @Override
                public void onComplete() {
                    if (!finished.compareAndSet(false, true)) {
                        return;
                    }
                    try {
                        Message assistantMessage = messageService.appendAssistantMessage(
                                conversation.getId(), buffer.toString());
                        send(emitter, "message.end", Map.of(
                                "messageId", assistantMessage.getId(),
                                "runId", run.getId()));
                        finishRun(run, AgentRunStatus.COMPLETED, "chars=" + buffer.length(), null, startedAt);
                        analyzeQuietly(userMessage);
                    } finally {
                        emitter.complete();
                    }
                }

                @Override
                public void onError(int code, String message) {
                    if (!finished.compareAndSet(false, true)) {
                        return;
                    }
                    send(emitter, "message.error", Map.of(
                            "runId", run.getId(),
                            "code", code,
                            "message", message));
                    finishRun(run, AgentRunStatus.FAILED, null, message, startedAt);
                    emitter.complete();
                }
            });

        } catch (Exception e) {
            log.error("chat stream failed, runId={}", run.getId(), e);
            if (finished.compareAndSet(false, true)) {
                send(emitter, "message.error", Map.of(
                        "runId", run.getId(),
                        "code", 500,
                        "message", "生成回复失败"));
                finishRun(run, AgentRunStatus.FAILED, null, e.toString(), startedAt);
                emitter.complete();
            }
        }
    }

    /**
     * 让学习闭环跟上：这次对话里提到的 Concept 会产生 Evidence。
     * <p>
     * 分析失败绝不能影响已经推完的对话 —— 用户已经拿到回复了，回滚没有意义。
     */
    private void analyzeQuietly(Message userMessage) {
        try {
            learningAnalysisService.analyzeMessage(userMessage);
        } catch (Exception e) {
            log.error("analyze message failed, messageId={}", userMessage.getId(), e);
        }
    }

    // ================= agent_run =================

    private AgentRun createRun(Long userId, Long conversationId, String content) {
        AgentRun run = new AgentRun();
        run.setUserId(userId);
        run.setConversationId(conversationId);
        run.setRunType(AgentRunType.CHAT);
        run.setStatus(AgentRunStatus.RUNNING);
        run.setInputSummary(truncate(content, 255));
        agentRunMapper.insert(run);
        return run;
    }

    /**
     * 收尾 agent_run。用只带 id 的空实体 updateById，MyBatis-Plus 默认忽略 null 字段，
     * 所以不会把创建时写好的 inputSummary 覆盖成空。
     */
    private void finishRun(AgentRun run, AgentRunStatus status, String outputSummary,
                           String error, LocalDateTime startedAt) {
        AgentRun patch = new AgentRun();
        patch.setId(run.getId());
        patch.setStatus(status);
        patch.setOutputSummary(outputSummary);
        patch.setErrorStack(truncate(error, 1000));
        patch.setFinishedAt(LocalDateTime.now());
        patch.setDurationMs((int) Duration.between(startedAt, patch.getFinishedAt()).toMillis());
        agentRunMapper.updateById(patch);
    }

    // ================= 内部工具 =================

    private void send(SseEmitter emitter, String event, Map<String, Object> payload) {
        try {
            emitter.send(SseEmitter.event()
                    .name(event)
                    .data(new LinkedHashMap<>(payload), MediaType.APPLICATION_JSON));
        } catch (IOException | IllegalStateException e) {
            // 客户端断开是常态，不值得刷错误日志
            log.debug("SSE send failed, event={}", event, e);
        }
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
