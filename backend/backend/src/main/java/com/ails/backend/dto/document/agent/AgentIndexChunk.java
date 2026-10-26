package com.ails.backend.dto.document.agent;

import java.util.Map;

/**
 * 已由 Spring Boot 写入 MySQL、带真实 chunk ID 的待索引块。
 */
public record AgentIndexChunk(
        Long chunkId,
        Integer chunkIndex,
        String content,
        Map<String, Object> metadata) {
}
