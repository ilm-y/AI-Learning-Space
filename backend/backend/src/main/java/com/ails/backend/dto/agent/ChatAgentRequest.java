package com.ails.backend.dto.agent;

import java.util.List;

/**
 * Backend → Agent Runtime 的一次对话请求。
 * <p>
 * 这是 Backend 与 Agent 之间唯一的入参边界：将来从 StubAgentGateway 换成
 * HttpPythonAgentGateway，只需要把这个对象（等价地）序列化过去，Controller 不用动。
 */
public record ChatAgentRequest(
        Long runId,
        Long userId,
        Long conversationId,
        Long learningSpaceId,
        /** 当前用户输入 */
        String userMessage,
        /** 该对话的历史（不含 userMessage） */
        List<ChatHistoryMessage> history) {
}
