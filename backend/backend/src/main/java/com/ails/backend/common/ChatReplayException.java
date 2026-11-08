package com.ails.backend.common;

import com.ails.backend.dto.conversation.ChatReplayResponse;

/**
 * 幂等回放 / 冲突的提前响应信号。
 * <p>
 * 正常情况下发消息返回 {@code SseEmitter}（{@code text/event-stream}）。但重复请求
 * 必须在建立 SSE 之前就返回明确的 HTTP 状态和 JSON 响应体：A 状态要 202 而不是
 * 200，否则客户端会以为又成功生成了一轮。Spring MVC 的返回值处理器按方法声明类型
 * 选择，同一个方法无法同时返回 {@code SseEmitter} 和 JSON，所以这里用异常把控制权
 * 交给 {@link GlobalExceptionHandler}，由它写出 JSON。
 * <p>
 * 这不是错误路径：202 / 200 都是幂等命中的正常结果，异常只承担「提前返回」的职责，
 * 因此全局处理器不会把它当未知异常记录 error 日志。
 */
public class ChatReplayException extends RuntimeException {

    private final int status;
    private final ChatReplayResponse response;

    public ChatReplayException(int status, ChatReplayResponse response) {
        super(response == null ? "chat replay" : response.message());
        this.status = status;
        this.response = response;
    }

    public int getStatus() {
        return status;
    }

    public ChatReplayResponse getResponse() {
        return response;
    }
}
