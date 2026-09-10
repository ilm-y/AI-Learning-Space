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

    @GetMapping("/{conceptId}")
    public ApiResponse<UnderstandingResponse> get(@PathVariable Long conceptId) {
        return ApiResponse.ok(understandingService.get(SecurityUtil.currentUserId(), conceptId));
    }

    @GetMapping("/{conceptId}/revisions")
    public ApiResponse<List<UnderstandingRevisionResponse>> revisions(@PathVariable Long conceptId) {
        return ApiResponse.ok(understandingService.revisions(SecurityUtil.currentUserId(), conceptId));
    }
}
