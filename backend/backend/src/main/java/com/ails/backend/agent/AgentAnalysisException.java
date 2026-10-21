package com.ails.backend.agent;

/**
 * 调用 Agent 分析接口失败。
 * <p>
 * 与 Chat 链路不同，分析失败<b>不面向用户</b>：调用方会退回本地 {@code rules-v1}，
 * 所以异常只需要带够排查信息（状态码 + 原因），由调用方记录日志。
 * <p>
 * 状态码沿用 HTTP 语义：
 * <ul>
 *   <li>503 —— Agent 进程不可达 / 超时；</li>
 *   <li>502 —— 上游返回错误或响应不符合契约；</li>
 *   <li>500 —— 本地序列化等非预期错误。</li>
 * </ul>
 */
public class AgentAnalysisException extends RuntimeException {

    private final int statusCode;

    public AgentAnalysisException(int statusCode, String message) {
        super(message);
        this.statusCode = statusCode;
    }

    public AgentAnalysisException(int statusCode, String message, Throwable cause) {
        super(message, cause);
        this.statusCode = statusCode;
    }

    public int statusCode() {
        return statusCode;
    }
}
