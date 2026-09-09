package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.highlight.HighlightRequest;
import com.ails.backend.dto.highlight.HighlightResponse;
import com.ails.backend.service.HighlightService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 高亮标记（在 AI 回复上标记重点 / 疑问 / 我的理解）。
 */
@RestController
@RequiredArgsConstructor
public class HighlightController {

    private final HighlightService highlightService;

    @PostMapping("/api/messages/{messageId}/highlights")
    public ApiResponse<HighlightResponse> create(@PathVariable Long messageId,
                                                 @Valid @RequestBody HighlightRequest request) {
        return ApiResponse.ok("标记成功",
                highlightService.create(SecurityUtil.currentUserId(), messageId, request));
    }

    @GetMapping("/api/highlights")
    public ApiResponse<List<HighlightResponse>> listMine() {
        return ApiResponse.ok(highlightService.listMine(SecurityUtil.currentUserId()));
    }
}
