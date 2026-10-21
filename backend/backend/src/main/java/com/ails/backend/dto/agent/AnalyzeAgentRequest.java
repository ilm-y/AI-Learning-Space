package com.ails.backend.dto.agent;

import java.util.List;

/**
 * Backend → Agent 的一次学习分析请求（{@code POST /agent/analyze}）。
 * <p>
 * 与 {@link ChatAgentRequest} 的关键差别：
 * <ul>
 *   <li>发生在回答<b>之后</b>，所以带 {@code assistantMessage} —— 分析器需要判断
 *       「这一轮对话是否真的产生了学习信号」，只看用户提问会把一次普通问答也当成理解证据；</li>
 *   <li>带 {@code messageId}，因为写入的 Evidence 必须能指回原始消息（evidence_source）；</li>
 *   <li>不要求 {@code session} 渲染，但保留字段让 Agent 侧契约与 Chat 一致。</li>
 * </ul>
 * <p>
 * <b>响应不是 SSE。</b> 分析结果是一份结构化 JSON，调用方要等它完整返回后在一个事务里
 * 写库；失败时调用方退回本地 {@code rules-v1}，用户不会看到这条链路上的错误。
 */
public record AnalyzeAgentRequest(
        Long runId,

        /** 跨进程关联键，与 Chat 用的是同一套生成规则（{@code utils.TraceIds}）。 */
        String traceId,

        Long userId,
        Long conversationId,
        Long learningSpaceId,

        /** 本轮用户消息的数据库 id，Evidence 的来源就是它。 */
        Long messageId,

        /** 本轮用户输入（不含助手回答）。 */
        String userMessage,

        /** 本轮助手回答；可为空（例如流式生成失败后仍尝试分析用户消息）。 */
        String assistantMessage,

        /** 本轮之前的对话历史，供 Agent 识别「那它呢？」这类指代。 */
        List<ChatHistoryMessage> history,

        ChatAgentRequest.SessionContext session,

        /** 用户自己的 JWT，供 Agent 回调后端只读检索接口。 */
        String authToken) {
}
