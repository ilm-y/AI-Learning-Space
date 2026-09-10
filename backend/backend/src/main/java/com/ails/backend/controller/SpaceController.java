package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.concept.ConceptRequest;
import com.ails.backend.dto.concept.ConceptResponse;
import com.ails.backend.dto.space.LearningSpaceRequest;
import com.ails.backend.dto.space.LearningSpaceResponse;
import com.ails.backend.service.ConceptService;
import com.ails.backend.service.LearningSpaceService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 学习空间及其知识点子资源。
 * <p>
 * 空间的对话子资源在 {@link ConversationController}。
 */
@RestController
@RequestMapping("/api/spaces")
@RequiredArgsConstructor
public class SpaceController {

    private final LearningSpaceService spaceService;
    private final ConceptService conceptService;

    @GetMapping
    public ApiResponse<List<LearningSpaceResponse>> listMine() {
        return ApiResponse.ok(spaceService.listMine(SecurityUtil.currentUserId()));
    }

    @PostMapping
    public ApiResponse<LearningSpaceResponse> create(@Valid @RequestBody LearningSpaceRequest request) {
        return ApiResponse.ok("创建成功", spaceService.create(SecurityUtil.currentUserId(), request));
    }

    @GetMapping("/{id}")
    public ApiResponse<LearningSpaceResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(spaceService.get(SecurityUtil.currentUserId(), id));
    }

    @PutMapping("/{id}")
    public ApiResponse<LearningSpaceResponse> update(@PathVariable Long id,
                                                     @Valid @RequestBody LearningSpaceRequest request) {
        return ApiResponse.ok(spaceService.update(SecurityUtil.currentUserId(), id, request));
    }

    @DeleteMapping("/{id}")
    public ApiResponse<Void> delete(@PathVariable Long id) {
        spaceService.delete(SecurityUtil.currentUserId(), id);
        return ApiResponse.ok("删除成功", null);
    }

    @GetMapping("/{id}/concepts")
    public ApiResponse<List<ConceptResponse>> listConcepts(@PathVariable Long id) {
        return ApiResponse.ok(conceptService.listBySpace(SecurityUtil.currentUserId(), id));
    }

    @PostMapping("/{id}/concepts")
    public ApiResponse<ConceptResponse> createConcept(@PathVariable Long id,
                                                      @Valid @RequestBody ConceptRequest request) {
        return ApiResponse.ok("创建成功", conceptService.createInSpace(SecurityUtil.currentUserId(), id, request));
    }
}
