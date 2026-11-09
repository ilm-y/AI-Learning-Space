package com.ails.backend.dto.agent;

import java.util.List;

/**
 * Manifest 分页查询响应。
 * <p>
 * 只做分页封装，{@code items} 内每条记录仍直接暴露 Manifest 原始结构。
 *
 * @param items 当前页记录，按 {@code created_at DESC, id DESC} 排序
 * @param total 满足过滤条件的总记录数
 * @param page  页码，从 1 开始
 * @param size  每页条数
 */
public record AgentRunManifestPageResponse(
        List<AgentRunManifestResponse> items,
        long total,
        int page,
        int size
) {
    public AgentRunManifestPageResponse {
        items = items == null ? List.of() : List.copyOf(items);
    }
}
