package com.ails.backend.mq;

import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.dto.agent.ChatHistoryMessage;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.util.List;

/**
 * Spring Boot → RabbitMQ → Python Agent Worker 的分析任务消息。
 * <p>
 * 字段与 {@link com.ails.backend.dto.agent.AnalyzeAgentRequest} 保持一一对应，
 * 只多了两个信封字段（{@code schemaVersion} / {@code taskId}）。这样 Python 侧可以直接
 * 复用现有的 {@code AnalyzeRequest} 模型，不需要为 MQ 再造一套业务 DTO。
 *
 * <h3>为什么消息里带原文，而不是只带 id</h3>
 * 分析任务需要用户消息、助手回答与最近历史。它们都是刚刚由 ChatService 落库的同一份数据，
 * 随任务一起带过去可以避免 Worker 再发一次同步查询；同时<b>不携带 authToken</b> ——
 * 用户 JWT 会过期，而消息可能在队列里停留任意长时间。
 * Worker 回访后端只读接口时使用它自己的服务端凭据（见
 * {@link com.ails.backend.security.InternalServiceAuthenticationFilter}）。
 *
 * <h3>不放用户 JWT</h3>
 * 队列是持久化的，消息会落盘、可能被重放。把 Bearer JWT 放进消息等于把用户凭证的
 * 生命周期交给队列长度和消费者可用性决定。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AnalysisTaskMessage(
        /** 消息契约版本；消费者遇到不认识的版本必须拒绝，而不是猜字段。 */
        int schemaVersion,

        /** 任务 id，等于 {@code agent_run.id}，也是结果消息的关联键。 */
        Long taskId,

        /** 与 taskId 相同，保留该字段是为了让 Python 侧直接构造 {@code AnalyzeRequest}。 */
        Long runId,

        String traceId,
        Long userId,
        Long conversationId,
        Long learningSpaceId,

        /** 对话所属的学习会话；可为空（会话不是学习闭环的必需条件）。 */
        Long sessionId,

        /** 本轮用户消息 id，Evidence 的来源。 */
        Long messageId,

        /** 本轮助手消息 id；可为空（例如助手回复落库失败）。 */
        Long assistantMessageId,

        String userMessage,

        /** 本轮助手回答；可为空。 */
        String assistantMessage,

        /** 本轮之前的历史，已排除本轮的用户/助手消息。 */
        List<ChatHistoryMessage> history,

        ChatAgentRequest.SessionContext session) {

    public static final int SCHEMA_VERSION = 1;

    public AnalysisTaskMessage {
        history = history == null ? List.of() : List.copyOf(history);
    }
}
