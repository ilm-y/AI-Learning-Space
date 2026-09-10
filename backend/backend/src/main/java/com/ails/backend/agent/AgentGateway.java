package com.ails.backend.agent;

import com.ails.backend.dto.agent.ChatAgentRequest;

/**
 * Backend 与 Agent Runtime 之间唯一的边界。
 * <p>
 * 现在由 {@link StubAgentGateway} 提供桩实现；将来接入 Python Agent 时，
 * 新增一个 HttpPythonAgentGateway 即可，ChatController / ChatService 不需要改动。
 * <p>
 * 这就是为什么 Controller 里不需要留任何「以后要调 Python Agent」的注释 ——
 * 需要替换的点只有这一个接口的实现。
 */
public interface AgentGateway {

    /**
     * 请求一次流式回复。实现方需同步或异步地把结果推给 handler。
     */
    void streamReply(ChatAgentRequest request, AgentStreamHandler handler);
}
