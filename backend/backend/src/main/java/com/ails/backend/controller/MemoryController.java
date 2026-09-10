package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.service.MemoryService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * Agent 长期记忆。
 * <p>
 * Memory 用 {@code CANDIDATE → ACTIVE → ARCHIVED} 管理生命周期，没有物理删除：
 * DELETE 的语义是归档，且不会影响 Evidence 与 Understanding。
 */
@RestController
@RequestMapping("/api/memories")
@RequiredArgsConstructor
public class MemoryController {

    private final MemoryService memoryService;

    @GetMapping
    public ApiResponse<List<MemoryResponse>> list(@RequestParam(required = false) Long spaceId,
                                                  @RequestParam(required = false) Long conceptId,
                                                  @RequestParam(required = false) MemoryStatus status) {
        return ApiResponse.ok(memoryService.list(SecurityUtil.currentUserId(), spaceId, conceptId, status));
    }

    /** 记忆检索（第一版关键词匹配） */
    @GetMapping("/search")
    public ApiResponse<List<MemoryResponse>> search(@RequestParam(required = false) Long spaceId,
                                                    @RequestParam(required = false) String keyword) {
        return ApiResponse.ok(memoryService.search(SecurityUtil.currentUserId(), spaceId, keyword));
    }

    @GetMapping("/{id}")
    public ApiResponse<MemoryResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(memoryService.get(SecurityUtil.currentUserId(), id));
    }

    @PostMapping
    public ApiResponse<MemoryResponse> create(@Valid @RequestBody MemoryRequest request) {
        return ApiResponse.ok("已记住", memoryService.create(SecurityUtil.currentUserId(), request));
    }

    /** 更新内容，或传 status 推进生命周期（CANDIDATE → ACTIVE） */
    @PatchMapping("/{id}")
    public ApiResponse<MemoryResponse> update(@PathVariable Long id,
                                              @Valid @RequestBody MemoryRequest request) {
        return ApiResponse.ok(memoryService.update(SecurityUtil.currentUserId(), id, request));
    }

    /** 归档，不是删除 */
    @DeleteMapping("/{id}")
    public ApiResponse<Void> archive(@PathVariable Long id) {
        memoryService.archive(SecurityUtil.currentUserId(), id);
        return ApiResponse.ok("已归档", null);
    }
}
