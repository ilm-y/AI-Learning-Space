package com.ails.backend.dto.agent;

import com.fasterxml.jackson.databind.JsonNode;

import java.time.LocalDateTime;

/**
 * 一次 Agent run 的上下文 Manifest 只读响应（D4-4）。
 * <p>
 * {@code manifest} 直接暴露 D4-3 定义的结构：
 * {@code schema_version / policy_version / intent / sources / budget}，不做字段重命名
 * 或二次加工；唯一允许的加工是按查询参数过滤 {@code sources}（单次 run 查询时）。
 * <p>
 * 不包含正文、完整 prompt、query 或异常原文 —— Manifest 本身在 Agent 侧就已经
 * 按这个边界生成，落库与查询都只搬运它。
 *
 * @param runId          {@code agent_run.id}
 * @param conversationId 归属对话
 * @param spaceId        经对话证明的空间归属
 * @param status         {@code agent_run.status} 原值
 * @param createdAt      run 创建时间，供时间范围排查
 * @param manifest       D4-3 最小 Manifest 原始 JSON
 */
public record AgentRunManifestResponse(
        Long runId,
        Long conversationId,
        Long spaceId,
        String status,
        LocalDateTime createdAt,
        JsonNode manifest
) {
}
