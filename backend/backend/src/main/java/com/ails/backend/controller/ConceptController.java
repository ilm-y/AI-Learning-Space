package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.concept.ConceptRequest;
import com.ails.backend.dto.concept.ConceptResponse;
import com.ails.backend.service.ConceptService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 知识点自身的查询 / 修改 / 删除。
 * <p>
 * 空间下的创建与列表在 {@link SpaceController}（`/api/spaces/{spaceId}/concepts`）。
 * <p>
 * DELETE 是软删除：Concept 上挂着 Understanding 与 Evidence，物理删除会炸掉学习历史。
 * 已经产生的证据与理解快照不受影响。
 */
@RestController
@RequiredArgsConstructor
public class ConceptController {

    private final ConceptService conceptService;

    @GetMapping("/api/concepts/{id}")
    public ApiResponse<ConceptResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(conceptService.get(SecurityUtil.currentUserId(), id));
    }

    @PutMapping("/api/concepts/{id}")
    public ApiResponse<ConceptResponse> update(@PathVariable Long id,
                                               @Valid @RequestBody ConceptRequest request) {
        return ApiResponse.ok(conceptService.update(SecurityUtil.currentUserId(), id, request));
    }

    @DeleteMapping("/api/concepts/{id}")
    public ApiResponse<Void> delete(@PathVariable Long id) {
        conceptService.delete(SecurityUtil.currentUserId(), id);
        return ApiResponse.ok("删除成功", null);
    }
}
