package com.ails.backend.agent;

/**
 * Agent 流式响应的终态元数据。
 * <p>
 * {@code finishReason=length} 表示模型因为 Token 上限停止，已经推送的文本是
 * 部分内容；{@code truncated=true} 是 Agent 归一化后的结论，后端据此把这一轮
 * 标记为不完整，而不是当成正常完成。
 * <p>
 * Agent 的旧版本或测试替身不会带这些字段，此时按正常完成处理，保持协议兼容。
 *
 * @param finishReason 供应商的完成原因，未知时为 {@code null}
 * @param truncated    内容是否因长度上限被截断
 */
public record StreamCompletion(String finishReason, boolean truncated) {

    /** 没有截断元数据时的正常终态。 */
    public static StreamCompletion normal() {
        return new StreamCompletion(null, false);
    }
}
