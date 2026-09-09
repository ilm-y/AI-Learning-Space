package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.state.LearningEvidenceResponse;
import com.ails.backend.dto.state.LearningStateResponse;
import com.ails.backend.service.LearningStateService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 学习状态看板：掌握度总览 / 知识点详情 / 证据时间线。
 */
@RestController
@RequestMapping("/api/learning-state")
@RequiredArgsConstructor
public class LearningStateController {

    private final LearningStateService learningStateService;

    @GetMapping("/overview")
    public ApiResponse<List<LearningStateResponse>> overview() {
        return ApiResponse.ok(learningStateService.overview(SecurityUtil.currentUserId()));
    }

    @GetMapping("/concepts/{conceptId}")
    public ApiResponse<LearningStateResponse> conceptDetail(@PathVariable Long conceptId) {
        return ApiResponse.ok(learningStateService.conceptDetail(SecurityUtil.currentUserId(), conceptId));
    }

    @GetMapping("/concepts/{conceptId}/evidences")
    public ApiResponse<List<LearningEvidenceResponse>> evidences(@PathVariable Long conceptId) {
        return ApiResponse.ok(learningStateService.evidences(SecurityUtil.currentUserId(), conceptId));
    }
}
