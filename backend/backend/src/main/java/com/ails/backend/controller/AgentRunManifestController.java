package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.agent.AgentRunManifestPageResponse;
import com.ails.backend.dto.agent.AgentRunManifestResponse;
import com.ails.backend.service.AgentRunManifestService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;

/**
 * D4-4：Agent 上下文 Manifest 只读查询。
 * <p>
 * 路径放在 {@code /api/spaces/{spaceId}/...} 下，与项目其余“空间子资源”一致，
 * 从而复用 {@code LearningSpaceService.ownedSpace} 的归属校验；run 本身的
 * {@code user_id} 与对话的空间归属在 Service 里再校验一次。
 * <p>
 * 只读：没有写入、修改、删除入口。
 */
@RestController
@RequestMapping("/api/spaces/{spaceId}/agent-run-manifests")
@RequiredArgsConstructor
public class AgentRunManifestController {

    private final AgentRunManifestService manifestService;

    /**
     * 按 run_id 查询单次 run 的 Manifest。
     * <p>
     * {@code sourceType} 对应 Manifest source 的 {@code type} 字段（D4-3 字段名不变）；
     * 三个过滤条件同时给出时取交集。
     */
    @GetMapping("/{runId}")
    public ApiResponse<AgentRunManifestResponse> get(
            @PathVariable Long spaceId,
            @PathVariable Long runId,
            @RequestParam(required = false) String sourceType,
            @RequestParam(required = false) String injectionStatus,
            @RequestParam(required = false) String reason) {
        return ApiResponse.ok(manifestService.get(
                SecurityUtil.currentUserId(), spaceId, runId,
                sourceType, injectionStatus, reason));
    }

    /**
     * 按 policy_version + intent + 时间范围分页查询。
     * <p>
     * {@code from} 含、{@code to} 不含，格式为 ISO date-time
     * （例如 {@code 2026-11-01T00:00:00}）。
     */
    @GetMapping
    public ApiResponse<AgentRunManifestPageResponse> page(
            @PathVariable Long spaceId,
            @RequestParam(required = false) String policyVersion,
            @RequestParam(required = false) String intent,
            @RequestParam(required = false)
            @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false)
            @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int size) {
        return ApiResponse.ok(manifestService.page(
                SecurityUtil.currentUserId(), spaceId,
                policyVersion, intent, from, to, page, size));
    }
}
