package com.ails.backend.agent;

import com.ails.backend.dto.agent.AgentCitation;

import java.util.List;

/**
 * Agent 流式响应的终态元数据。
 * <p>
 * {@code finishReason=length} 表示模型因为 Token 上限停止，已经推送的文本是
 * 部分内容；{@code truncated=true} 是 Agent 归一化后的结论，后端据此把这一轮
 * 标记为不完整，而不是当成正常完成。
 * <p>
 * {@code citations} 是 Agent 校验通过后的结构化来源。旧版本 Agent 或测试替身
 * 不带这个字段（或给 null）时按空列表处理，保持协议兼容。
 * <p>
 * {@code contextManifest} 是 D4-3 定义的上下文 Manifest 原始 JSON（字符串）。
 * 它只含来源 id / 状态 / 原因 / token 估算，不含正文、完整 prompt、query 或异常原文。
 * 旧版本 Agent 不带这个字段时为 {@code null}，后端保持"没有可观测 Manifest"的语义，
 * 不伪造空对象。
 *
 * @param finishReason    供应商的完成原因，未知时为 {@code null}
 * @param truncated       内容是否因长度上限被截断
 * @param citations       结构化来源引用，永不为 {@code null}
 * @param contextManifest 最小上下文 Manifest 的原始 JSON，可能为 {@code null}
 */
public record StreamCompletion(String finishReason, boolean truncated, List<AgentCitation> citations,
                               String contextManifest) {

    public StreamCompletion {
        citations = citations == null ? List.of() : List.copyOf(citations);
    }

    /** 兼容旧调用方：没有 Manifest 的终态。 */
    public StreamCompletion(String finishReason, boolean truncated, List<AgentCitation> citations) {
        this(finishReason, truncated, citations, null);
    }

    /** 兼容旧调用方：没有引用与 Manifest 元数据的终态。 */
    public StreamCompletion(String finishReason, boolean truncated) {
        this(finishReason, truncated, List.of(), null);
    }

    /** 没有截断元数据时的正常终态。 */
    public static StreamCompletion normal() {
        return new StreamCompletion(null, false, List.of(), null);
    }
}
