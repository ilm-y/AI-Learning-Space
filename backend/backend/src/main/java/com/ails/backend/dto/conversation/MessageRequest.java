package com.ails.backend.dto.conversation;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 发送消息请求。
 * <p>
 * {@code clientRequestId} 是客户端生成的幂等键（UUID 字符串），与
 * {@code conversationId} 一起唯一标识一轮用户提问。服务端用
 * {@code UNIQUE(conversation_id, client_request_id)} 收敛重复请求：
 * 同一个 id 重放时不会再次落消息、也不会再创建 CHAT run。
 * <p>
 * <b>这是一个必填字段。</b>仓库内的 Web 客户端已同步发送；其它旧客户端只发
 * {@code content} 会收到 400。选择必填而不是服务端兜底生成：随机 id 会让每次重试
 * 都变成新一轮生成，幂等键形同虚设。
 * <p>
 * 长度限制 8–64 与数据库列宽一致：UUID 是 36 字符，同时允许其它客户端使用
 * 自己的请求标识；但不接受空串、纯空白或超长值。
 * <p>
 * {@code content} 上限 8000 字符是 DTO 层的第一道防线（按单轮提问的合理长度设定，
 * 避免把整篇文档当提问塞进 prompt）。真正的请求体大小限制应在 Web 入口
 * （{@code server.max-http-request-size} 或网关）完成，当前<b>尚未实现</b>，
 * 见 KNOWN_LIMITATIONS CHAT-01。
 */
public record MessageRequest(
        @NotBlank(message = "消息内容不能为空")
        @Size(max = 8000, message = "消息内容不能超过 8000 字符")
        String content,

        @NotBlank(message = "clientRequestId 不能为空")
        @Size(min = 8, max = 64, message = "clientRequestId 长度必须在 8 到 64 个字符之间")
        String clientRequestId
) {
}
