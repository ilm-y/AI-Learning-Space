package com.ails.backend.dto.memory.agent;

/**
 * {@code POST /agent/memories/sync} 响应数据。
 *
 * @param pointId    确定性 point id；DELETE 时为 {@code null}
 * @param collection Agent 实际使用的 Qdrant collection
 * @param skipped    内容与过滤字段都未变化，本次没有重嵌入
 */
public record MemorySyncResponse(
        String pointId,
        String collection,
        boolean skipped) {
}
