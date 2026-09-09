package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.state.ContinueLearningItem;
import com.ails.backend.service.LearningStateService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 首页仪表盘。
 */
@RestController
@RequestMapping("/api/dashboard")
@RequiredArgsConstructor
public class DashboardController {

    private final LearningStateService learningStateService;

    /**
     * 继续学习：返回按优先级/下次复习时间排序的待复习知识点。
     */
    @GetMapping("/continue-learning")
    public ApiResponse<List<ContinueLearningItem>> continueLearning(
            @RequestParam(defaultValue = "10") int limit) {
        return ApiResponse.ok(learningStateService.continueLearning(SecurityUtil.currentUserId(), limit));
    }
}
