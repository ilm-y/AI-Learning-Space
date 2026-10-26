package com.ails.backend.dto.document.agent;

import java.util.Map;

/**
 * Agent 解析接口返回的单个文本块。字段名与 FastAPI 响应保持 camelCase 一致。
 */
public record AgentParsedChunk(
        Integer chunkIndex,
        String content,
        Integer tokenCount,
        Boolean tokenCountEstimated,
        Map<String, Object> metadata) {
}
