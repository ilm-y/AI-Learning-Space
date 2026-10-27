package com.ails.backend.dto.document.agent;

import java.util.List;

/**
 * {@code POST /agent/documents/parse} 的 data 部分。
 */
public record AgentParseResponse(
        String parserVersion,
        List<AgentParsedChunk> chunks) {
}
