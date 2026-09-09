package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.verification.AnswerRequest;
import com.ails.backend.dto.verification.AnswerResult;
import com.ails.backend.dto.verification.VerificationTaskResponse;
import com.ails.backend.service.VerificationService;
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
 * 主动验证：待答列表 / 提交答案（M1 本地闭环）。
 */
@RestController
@RequestMapping("/api/verification")
@RequiredArgsConstructor
public class VerificationController {

    private final VerificationService verificationService;

    @GetMapping("/pending")
    public ApiResponse<List<VerificationTaskResponse>> pending() {
        return ApiResponse.ok(verificationService.listPending(SecurityUtil.currentUserId()));
    }

    @PostMapping("/{taskId}/answer")
    public ApiResponse<AnswerResult> answer(@PathVariable Long taskId,
                                            @Valid @RequestBody AnswerRequest request) {
        return ApiResponse.ok(verificationService.answer(SecurityUtil.currentUserId(), taskId, request));
    }
}
