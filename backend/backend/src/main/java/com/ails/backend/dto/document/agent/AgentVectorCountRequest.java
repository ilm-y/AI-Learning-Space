package com.ails.backend.dto.document.agent;

/**
 * {@code POST /agent/documents/vector-count} 请求体。
 *
 * <p>{@code collection} 必须是该活跃版本实际写入的 collection。后端在无法定位
 * collection（legacy 文档）时不会调用本接口，因此 Agent 端不做“按当前配置猜测”。</p>
 */
public record AgentVectorCountRequest(
        Long documentId,
        String collection,
        Integer indexVersion) {
}
