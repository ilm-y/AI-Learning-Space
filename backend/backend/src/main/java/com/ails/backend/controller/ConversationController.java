package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.conversation.ConversationRequest;
import com.ails.backend.dto.conversation.ConversationResponse;
import com.ails.backend.service.ConversationService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 对话资源：空间的子资源创建 + 对话自身的查询 / 改名 / 归档 / 删除。
 * <p>
 * 对话内的消息收发在 {@link ChatController}。
 * <p>
 * DELETE 是软删除 —— 对话是学习历史的容器，物理删除会一并毁掉证据链。
 */
@RestController
@RequiredArgsConstructor
public class ConversationController {

    private final ConversationService conversationService;

    @GetMapping("/api/spaces/{spaceId}/conversations")
    public ApiResponse<List<ConversationResponse>> listBySpace(@PathVariable Long spaceId) {
        return ApiResponse.ok(conversationService.listBySpace(SecurityUtil.currentUserId(), spaceId));
    }

    @PostMapping("/api/spaces/{spaceId}/conversations")
    public ApiResponse<ConversationResponse> createInSpace(
            @PathVariable Long spaceId,
            @Valid @RequestBody(required = false) ConversationRequest request) {
        return ApiResponse.ok("创建成功", conversationService.createInSpace(
                SecurityUtil.currentUserId(), spaceId,
                request == null ? new ConversationRequest(null) : request));
    }

    @GetMapping("/api/conversations/{id}")
    public ApiResponse<ConversationResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(conversationService.get(SecurityUtil.currentUserId(), id));
    }

    @PutMapping("/api/conversations/{id}")
    public ApiResponse<ConversationResponse> updateTitle(@PathVariable Long id,
                                                         @RequestBody(required = false) ConversationRequest request) {
        return ApiResponse.ok(conversationService.updateTitle(SecurityUtil.currentUserId(), id,
                request == null ? new ConversationRequest(null) : request));
    }

    /** 归档：对话不再接受新消息，但历史与证据保留 */
    @PostMapping("/api/conversations/{id}/archive")
    public ApiResponse<ConversationResponse> archive(@PathVariable Long id) {
        return ApiResponse.ok("已归档", conversationService.archive(SecurityUtil.currentUserId(), id));
    }

    @DeleteMapping("/api/conversations/{id}")
    public ApiResponse<Void> delete(@PathVariable Long id) {
        conversationService.delete(SecurityUtil.currentUserId(), id);
        return ApiResponse.ok("删除成功", null);
    }
}
