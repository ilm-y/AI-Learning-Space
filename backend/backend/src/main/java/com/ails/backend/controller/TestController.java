package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.test.AnswerRequest;
import com.ails.backend.dto.test.AnswerResult;
import com.ails.backend.dto.test.TestTaskRequest;
import com.ails.backend.dto.test.TestTaskResponse;
import com.ails.backend.service.TestService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 主动验证：出题 / 待答列表 / 提交答案。
 * <p>
 * 测试嵌在对话里，不是独立的考试页面。提交答案后判分结果会成为一条 Evidence，
 * 推动 Understanding 演进 —— 响应里的 status 就是这次回答之后的最新理解状态。
 */
@RestController
@RequestMapping("/api/tests")
@RequiredArgsConstructor
public class TestController {

    private final TestService testService;

    @PostMapping
    public ApiResponse<TestTaskResponse> create(@Valid @RequestBody TestTaskRequest request) {
        return ApiResponse.ok("出题成功", testService.create(SecurityUtil.currentUserId(), request));
    }

    @GetMapping("/pending")
    public ApiResponse<List<TestTaskResponse>> listPending() {
        return ApiResponse.ok(testService.listPending(SecurityUtil.currentUserId()));
    }

    @GetMapping("/{testId}")
    public ApiResponse<TestTaskResponse> get(@PathVariable Long testId) {
        return ApiResponse.ok(testService.get(SecurityUtil.currentUserId(), testId));
    }

    @PostMapping("/{testId}/answer")
    public ApiResponse<AnswerResult> answer(@PathVariable Long testId,
                                            @Valid @RequestBody AnswerRequest request) {
        return ApiResponse.ok(testService.answer(SecurityUtil.currentUserId(), testId, request));
    }
}
