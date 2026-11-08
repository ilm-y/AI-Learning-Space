package com.ails.backend.dto.document.agent;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * {@code POST /agent/documents/vector-count} 响应体。
 *
 * <p>{@code collectionExists=false} 表示 collection 已被删除：这是“Qdrant 可达但
 * 数据缺失”，应与网络不可达（HTTP 503/504）区分。后者由客户端抛
 * {@link com.ails.backend.service.DocumentIndexException} 表达，不会走到这里。</p>
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AgentVectorCountResponse(
        String collection,
        boolean collectionExists,
        long count) {
}
