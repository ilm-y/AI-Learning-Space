package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.conversation.MessageRequest;
import com.ails.backend.dto.conversation.MessageResponse;
import com.ails.backend.service.ChatService;
import com.ails.backend.service.MessageService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.List;

/**
 * 对话内的消息收发。
 * <p>
 * 发消息是流式接口，返回 {@code text/event-stream}，事件契约见 {@link ChatService}：
 * {@code message.start / message.delta / message.end / message.error}。
 * 前端要用支持流式的 HTTP 客户端（Flutter 侧是 http 的 StreamedResponse），
 * 普通 fetch 拿不到中间态。
 */
@RestController
@RequestMapping("/api/conversations")
@RequiredArgsConstructor
public class ChatController {

    private final ChatService chatService;
    private final MessageService messageService;

    @GetMapping(value = "/{id}/messages", produces = MediaType.APPLICATION_JSON_VALUE)
    public ApiResponse<List<MessageResponse>> listMessages(@PathVariable Long id) {
        return ApiResponse.ok(messageService.listByConversation(SecurityUtil.currentUserId(), id));
    }

    /**
     * 发送用户消息并以 SSE 流式返回助手回复。
     * <p>
     * 首次请求返回 {@code text/event-stream}；同一 {@code clientRequestId} 的重放
     * 会在建立 SSE 之前由 {@link com.ails.backend.common.GlobalExceptionHandler}
     * 写成 JSON（202 处理中 / 200 已完成 / 409 失败或冲突），不会重新生成。
     * <p>
     * 注意返回类型是 SseEmitter 而不是 ApiResponse —— 流式接口不能用统一响应体包，
     * 失败信息走 {@code message.error} 事件。
     */
    @PostMapping(value = "/{id}/messages", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter sendMessage(@PathVariable Long id, @Valid @RequestBody MessageRequest request) {
        // token 在这里取下并一路带到 ChatAgentRequest：Agent 要用它回访后端的只读检索接口，
        // 换成在 Service 里现取是不可能的 —— SSE 的生成线程上没有 SecurityContext（见 ChatService.stream）。
        return chatService.stream(SecurityUtil.currentUserId(), id, request.content(),
                request.clientRequestId(), SecurityUtil.currentToken());
    }
}
