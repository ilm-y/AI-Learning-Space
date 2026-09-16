package com.ails.backend.dto.agent;

import java.util.List;

/**
 * Backend → Agent Runtime 的一次对话请求。
 * <p>
 * 这是 Backend 与 Agent 之间唯一的入参边界：将来从 StubAgentGateway 换成
 * HttpPythonAgentGateway，只需要把这个对象（等价地）序列化过去，Controller 不用动。
 * <p>
 * 字段名是 camelCase，Agent 侧（{@code agent/app/schemas.py}）用 alias 生成器接住了同一套命名，
 * 两边不需要任何手工映射。
 */
public record ChatAgentRequest(
        Long runId,
        Long userId,
        Long conversationId,
        Long learningSpaceId,
        /** 当前用户输入 */
        String userMessage,
        /** 该对话的历史（不含 userMessage） */
        List<ChatHistoryMessage> history,
        /**
         * 当前登录用户的原始 JWT，供 Agent 回访后端的鉴权只读接口时透传。
         * <p>
         * 这不是「可选的优化项」：Agent 的 HTTP 检索线（Concept / Understanding / Memory / Evidence）
         * 全部按登录用户鉴权，没有它就只能降级到 Fixture —— 检索看起来接好了，实际一次都没跑。
         * 缺失时 Agent 侧不会报错，只会静默降级，所以这里宁可是必填字段。
         */
        String authToken) {
}
