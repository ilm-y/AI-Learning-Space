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

        /**
         * 跨进程串联这次执行的关联键，由后端 {@code ChatService.createRun} 生成。
         * <p>
         * 这个项目是两跳（后端 → Agent），runId 只在各自进程里有效，traceId 才能把
         * 后端日志里的这一条和 Agent 日志里的那一条对起来。
         */
        String traceId,

        Long userId,
        Long conversationId,
        Long learningSpaceId,

        /** 当前用户输入 */
        String userMessage,

        /** 该对话的历史（不含 userMessage） */
        List<ChatHistoryMessage> history,

        /**
         * 这条对话归属的学习活动，可为 null。
         * <p>
         * <b>null 是常态，不是异常</b>：Session 是懒创建的，一条刚建出来的对话在
         * 第一条有效学习消息之前没有任何归属；而第一条消息本身恰好就带着 null 出发 ——
         * 归属是这一轮刚刚才定下来的（见 {@code SessionService.resolveForConversation}）。
         * 所以 Agent 侧必须容忍它缺失，不能当成错误。
         */
        SessionContext session,

        /**
         * 当前登录用户的原始 JWT，供 Agent 回访后端的鉴权只读接口时透传。
         * <p>
         * 这不是「可选的优化项」：Agent 的 HTTP 检索线（Concept / Understanding / Memory / Evidence）
         * 全部按登录用户鉴权，没有它就只能降级到 Fixture —— 检索看起来接好了，实际一次都没跑。
         * 缺失时 Agent 侧不会报错，只会静默降级，所以这里宁可是必填字段。
         */
        String authToken) {

    /**
     * 学习活动的短期工作上下文（{@code docs/session-design.md} §一.4）。
     * <p>
     * 只带 Agent 需要的四个字段，不带 userId / status / 时间戳 ——
     * Agent 要回答的是「这一阶段用户想解决什么、现在卡在哪」，别的都是噪声。
     *
     * @param goal         这一阶段最终要解决什么（长期）
     * @param currentFocus 当前卡在哪里 / 正在解决什么（短期），可空
     */
    public record SessionContext(
            Long id,
            String title,
            String goal,
            String currentFocus) {
    }
}
