package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.service.EvidenceService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 证据链查询。
 * <p>
 * <b>只有读接口。</b>Evidence 是 append-only：它只被追加，永远不会被修改或删除，
 * 所以这里没有 PUT / DELETE。冲突的证据不是「把旧的改掉」，而是「再加一条」，
 * 让学习历史保持完整可追溯。
 */
@RestController
@RequiredArgsConstructor
public class EvidenceController {

    private final EvidenceService evidenceService;

    /**
     * 旧单概念接口：保持兼容，不改变既有前端与历史调用方语义。
     * <p>
     * 新调用方（Agent Evidence 检索）应使用下面的批量有界接口；
     * 旧接口没有 spaceId / limit，无法在一次查询里完成空间校验与有界裁剪。
     */
    @GetMapping("/api/understandings/{conceptId}/evidence")
    public ApiResponse<List<EvidenceResponse>> listByConcept(@PathVariable Long conceptId) {
        return ApiResponse.ok(evidenceService.listByConcept(SecurityUtil.currentUserId(), conceptId));
    }

    /**
     * 跨概念的有界证据查询（Agent 专用读路径）。
     * <p>
     * {@code conceptIds} 支持逗号分隔（{@code conceptIds=1,2}）或重复参数；
     * {@code limit} 由服务端封顶，默认 {@value EvidenceService#DEFAULT_RECENT_LIMIT}。
     * 空间与概念归属都会校验：跨用户空间 404，跨空间概念 404。
     */
    @GetMapping("/api/evidences")
    public ApiResponse<List<EvidenceResponse>> listRecent(
            @RequestParam Long spaceId,
            @RequestParam List<Long> conceptIds,
            @RequestParam(required = false) Integer limit) {
        return ApiResponse.ok(evidenceService.listRecentByConcepts(
                SecurityUtil.currentUserId(), spaceId, conceptIds, limit));
    }

    /**
     * 单条证据详情，返回的 sources 可以跳回原始消息 / 批注 / 测试回答。
     */
    @GetMapping("/api/evidence/{evidenceId}")
    public ApiResponse<EvidenceResponse> get(@PathVariable Long evidenceId) {
        return ApiResponse.ok(evidenceService.get(SecurityUtil.currentUserId(), evidenceId));
    }
}
