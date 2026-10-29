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
 * 随任务一起带过去可以避免 Worker 再发一次同步查询；同时<b>不携带用户 JWT</b> ——
 * 用户 JWT 会过期，而消息可能在队列里停留任意长时间。
 *
 * <h3>为什么带 backendIdentity</h3>
 * Worker 回访后端只读接口时需要一个“代表该用户”的短声明。该声明必须由 Spring 用
 * 只存在于 Spring 的 delegation-secret 签发；Agent 只转发，不重新签发。把它随任务
 * 一起投递，是因为 Worker 没有用户 JWT，也不能靠自己持有的密钥生成后端接受的声明。
 * 声明带 audience、scope、有效期；过期后被后端拒绝，不会被降级信任。
 *
 * <h3>不放用户 JWT</h3>
 * 队列是持久化的，消息会落盘。把 Bearer JWT 放进消息等于把用户凭证的
 * 生命周期交给队列长度和消费者可用性决定；backendIdentity 是作用域受限、
 * 短期且不能由 Agent 伪造的委托声明，风险面小于用户 JWT，但仍按内部凭据保护。
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

        ChatAgentRequest.SessionContext session,

        /** Spring 签发的后端委托声明（aud=learning-backend, scope=analysis:read）。 */
        String backendIdentity) {

    /**
     * 契约版本。v2 起任务必须携带 backendIdentity；旧 Worker 不认识 v2 会拒绝，
     * 新 Worker 也不会为缺失声明的 v1 任务自行签发身份。
     */
    public static final int SCHEMA_VERSION = 2;

    public AnalysisTaskMessage {
        history = history == null ? List.of() : List.copyOf(history);
    }

    /**
     * 兼容旧测试/工具构造：不带委托声明。生产投递路径必须使用带 backendIdentity
     * 的构造器；缺少声明时 Worker 会 fail closed，而不是自行签发。
     */
    public AnalysisTaskMessage(int schemaVersion, Long taskId, Long runId, String traceId,
                               Long userId, Long conversationId, Long learningSpaceId,
                               Long sessionId, Long messageId, Long assistantMessageId,
                               String userMessage, String assistantMessage,
                               List<ChatHistoryMessage> history,
                               ChatAgentRequest.SessionContext session) {
        this(schemaVersion, taskId, runId, traceId, userId, conversationId, learningSpaceId,
                sessionId, messageId, assistantMessageId, userMessage, assistantMessage,
                history, session, null);
    }
}
