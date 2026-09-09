package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.conversation.ConversationRequest;
import com.ails.backend.dto.conversation.ConversationResponse;
import com.ails.backend.dto.conversation.MessageRequest;
import com.ails.backend.dto.conversation.MessageResponse;
import com.ails.backend.service.ConversationService;
import com.ails.backend.service.MessageService;
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
 * 对话内的消息收发（聊天）。
 */
@RestController
@RequestMapping("/api/conversations")
@RequiredArgsConstructor
public class ChatController {

    private final ConversationService conversationService;
    private final MessageService messageService;

    @GetMapping("/{id}")
    public ApiResponse<ConversationResponse> getConversation(@PathVariable Long id) {
        return ApiResponse.ok(conversationService.get(SecurityUtil.currentUserId(), id));
    }

    @PutMapping("/{id}")
    public ApiResponse<ConversationResponse> updateConversation(@PathVariable Long id,
                                                                @RequestBody(required = false) ConversationRequest request) {
        return ApiResponse.ok(conversationService.updateTitle(SecurityUtil.currentUserId(), id,
                request == null ? new ConversationRequest(null) : request));
    }

    @DeleteMapping("/{id}")
    public ApiResponse<Void> deleteConversation(@PathVariable Long id) {
        conversationService.delete(SecurityUtil.currentUserId(), id);
        return ApiResponse.ok("删除成功", null);
    }

    @GetMapping("/{id}/messages")
    public ApiResponse<List<MessageResponse>> listMessages(@PathVariable Long id) {
        return ApiResponse.ok(messageService.listByConversation(SecurityUtil.currentUserId(), id));
    }

    /**
     * 发送用户消息（M1 仅落库；SSE / AI 回复见 MessageService TODO）。
     */
    @PostMapping("/{id}/messages")
    public ApiResponse<MessageResponse> sendMessage(@PathVariable Long id,
                                                    @Valid @RequestBody MessageRequest request) {
        return ApiResponse.ok(messageService.sendUserMessage(SecurityUtil.currentUserId(), id, request));
    }
}
