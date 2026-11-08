package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.understanding.UnderstandingResponse;
import com.ails.backend.dto.understanding.UnderstandingRevisionResponse;
import com.ails.backend.service.UnderstandingService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 理解快照与它的历史。
 * <p>
 * <b>只有读接口。</b>理解不能被直接改，也不能被删 —— 它只能通过新的证据演进，
 * 每次演进都会在 {@code /revisions} 里留下一条不可变的记录。
 * 「为什么系统认为我理解了」这个问题的答案就在 revisions 里。
 */
@RestController
@RequestMapping("/api/understandings")
@RequiredArgsConstructor
public class UnderstandingController {

    private final UnderstandingService understandingService;

    @GetMapping
    public ApiResponse<List<UnderstandingResponse>> listBySpace(@RequestParam Long spaceId) {
        return ApiResponse.ok(understandingService.listBySpace(SecurityUtil.currentUserId(), spaceId));
    }

    /**
     * 当前理解快照。
     * <p>
     * {@code spaceId} 为可选参数：传入时额外断言该概念属于该学习空间，跨 Space 一律 404。
     * 不传时保持原有行为，兼容历史调用方。
     */
    @GetMapping("/{conceptId}")
    public ApiResponse<UnderstandingResponse> get(
            @PathVariable Long conceptId,
            @RequestParam(required = false) Long spaceId) {
        return ApiResponse.ok(
                understandingService.get(SecurityUtil.currentUserId(), conceptId, spaceId));
    }

    /**
     * 理解历史版本。{@code spaceId} 语义与 {@link #get(Long, Long)} 相同，同样是可选参数。
     */
    @GetMapping("/{conceptId}/revisions")
    public ApiResponse<List<UnderstandingRevisionResponse>> revisions(
            @PathVariable Long conceptId,
            @RequestParam(required = false) Long spaceId) {
        return ApiResponse.ok(
                understandingService.revisions(SecurityUtil.currentUserId(), conceptId, spaceId));
    }
}
