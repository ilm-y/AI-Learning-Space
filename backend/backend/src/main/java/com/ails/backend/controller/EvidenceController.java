package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.understanding.EvidenceResponse;
import com.ails.backend.service.EvidenceService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
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

    @GetMapping("/api/understandings/{conceptId}/evidence")
    public ApiResponse<List<EvidenceResponse>> listByConcept(@PathVariable Long conceptId) {
        return ApiResponse.ok(evidenceService.listByConcept(SecurityUtil.currentUserId(), conceptId));
    }

    /**
     * 单条证据详情，返回的 sources 可以跳回原始消息 / 批注 / 测试回答。
     */
    @GetMapping("/api/evidence/{evidenceId}")
    public ApiResponse<EvidenceResponse> get(@PathVariable Long evidenceId) {
        return ApiResponse.ok(evidenceService.get(SecurityUtil.currentUserId(), evidenceId));
    }
}
