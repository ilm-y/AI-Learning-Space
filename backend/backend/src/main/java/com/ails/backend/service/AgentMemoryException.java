package com.ails.backend.service;

/**
 * Agent 记忆向量接口的失败。
 *
 * <p>Outbox Relay 用它区分两类失败：</p>
 * <ul>
 *   <li>{@link #isPermanent()}：4xx（408/429 除外）表示请求本身有问题，
 *       重试没有意义，应直接进入 DEAD；</li>
 *   <li>其余（5xx、超时、连接失败、契约错误）可退避重试。</li>
 * </ul>
 */
public class AgentMemoryException extends RuntimeException {

    private final int statusCode;
    private final boolean permanent;

    public AgentMemoryException(int statusCode, String message) {
        this(statusCode, message, null, false);
    }

    public AgentMemoryException(int statusCode, String message, Throwable cause) {
        this(statusCode, message, cause, false);
    }

    public AgentMemoryException(int statusCode, String message, Throwable cause,
                                boolean permanent) {
        super(message, cause);
        this.statusCode = statusCode;
        this.permanent = permanent;
    }

    public int statusCode() {
        return statusCode;
    }

    /**
     * 4xx 契约/鉴权错误不可重试；响应契约漂移（即使状态码是 502）也标记为
     * permanent：字段改名只能靠发布修复，重试只会浪费队列。
     * 408（超时）与 429（限流）虽是 4xx，但明确是暂时性的，按可重试处理。
     */
    public boolean isPermanent() {
        if (permanent) {
            return true;
        }
        return statusCode >= 400 && statusCode < 500
                && statusCode != 408 && statusCode != 429;
    }
}
