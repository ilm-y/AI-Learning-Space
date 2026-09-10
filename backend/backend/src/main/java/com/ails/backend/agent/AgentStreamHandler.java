package com.ails.backend.agent;

/**
 * Agent 流式输出的回调。
 * <p>
 * AgentGateway 只负责把内容「推出来」，至于推到 SSE、WebSocket 还是别的地方，
 * 由调用方（ChatService）决定。这样换传输层不用动 Agent 侧。
 */
public interface AgentStreamHandler {

    /** 增量文本 */
    void onDelta(String delta);

    /** 流正常结束 */
    void onComplete();

    /** 流异常结束 */
    void onError(int code, String message);
}
