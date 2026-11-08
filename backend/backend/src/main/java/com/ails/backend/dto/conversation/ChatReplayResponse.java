package com.ails.backend.dto.conversation;

/**
 * 重复 POST 的幂等回放响应。
 * <p>
 * 只在 {@code (conversationId, clientRequestId)} 已存在时返回；首次请求仍然走
 * {@code text/event-stream}，本响应不会出现在新建路径上。
 * <p>
 * 字段说明：
 * <ul>
 *   <li>{@code idempotentReplay} —— 固定为 true，表明服务端没有重新生成；</li>
 *   <li>{@code state} —— {@link #STATE_PROCESSING} / {@link #STATE_COMPLETED} /
 *       {@link #STATE_TRUNCATED} / {@link #STATE_FAILED} /
 *       {@link #STATE_INCONSISTENT} / {@link #STATE_CLIENT_REQUEST_CONFLICT}；</li>
 *   <li>{@code userMessageId} —— 命中的 USER 消息；</li>
 *   <li>{@code runId} —— 命中的 CHAT run；状态 C（无 run）时为 null；</li>
 *   <li>{@code assistantMessageId} —— 已持久化的助手消息，处理中 / 失败 / 无 run 时为 null；</li>
 *   <li>{@code runStatus} —— {@code agent_run.status} 原值，便于排查；无 run 时为 null；</li>
 *   <li>{@code message} —— 面向客户端的简短说明。</li>
 * </ul>
 * 没有套 {@code ApiResponse}：这是一个独立的状态语义，不是一个「成功/失败」二分的
 * 统一响应；HTTP 状态码本身已经表达了 202 / 200 / 409。
 */
public record ChatReplayResponse(
        boolean idempotentReplay,
        String state,
        Long conversationId,
        Long userMessageId,
        Long runId,
        Long assistantMessageId,
        String runStatus,
        String message
) {

    /** 已有 USER 消息 + run 仍处于非终态：请求正在处理。 */
    public static final String STATE_PROCESSING = "PROCESSING";
    /** 已有 USER + ASSISTANT 消息，run 正常完成。 */
    public static final String STATE_COMPLETED = "COMPLETED";
    /** 已有 USER + ASSISTANT 消息，但回答因 Token 上限被截断。 */
    public static final String STATE_TRUNCATED = "TRUNCATED";
    /** 原请求已失败且没有可回放的助手消息。 */
    public static final String STATE_FAILED = "FAILED";
    /** 数据不一致：有 USER 消息但没有 run，或 run 终态却找不到助手消息。 */
    public static final String STATE_INCONSISTENT = "INCONSISTENT";
    /** 同一 clientRequestId 被用于不同内容。 */
    public static final String STATE_CLIENT_REQUEST_CONFLICT = "CLIENT_REQUEST_CONFLICT";
}
