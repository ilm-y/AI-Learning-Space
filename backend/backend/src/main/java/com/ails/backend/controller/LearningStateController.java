package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.state.AttentionItem;
import com.ails.backend.dto.state.LearningStateResponse;
import com.ails.backend.dto.state.TopicCoverageItem;
import com.ails.backend.service.LearningStateService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 学习状态查询。
 * <p>
 * <b>只有查询接口，没有修改入口。</b>LearningState 是 Understanding 的物化投影，
 * 由 {@link LearningStateService} 在证据链路末端单向派生。
 * 想让状态变化，唯一的方式是产生新的证据，而不是调一个「改状态」的接口。
 * <p>
 * 这里也没有掌握度分数 —— 状态是 5 个离散值，想知道依据去看 Understanding 的 Revision。
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

    /** 现在最该关注什么：误解 > 久未验证 > 到期待复习 */
    @GetMapping("/attention")
    public ApiResponse<List<AttentionItem>> attention(@RequestParam(defaultValue = "10") int limit) {
        return ApiResponse.ok(learningStateService.attention(SecurityUtil.currentUserId(), limit));
    }

    /** 某个学习空间的知识点覆盖情况 */
    @GetMapping("/topic-coverage")
    public ApiResponse<List<TopicCoverageItem>> topicCoverage(@RequestParam Long spaceId) {
        return ApiResponse.ok(learningStateService.topicCoverage(SecurityUtil.currentUserId(), spaceId));
    }
}
