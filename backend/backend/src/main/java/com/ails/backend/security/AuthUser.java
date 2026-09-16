package com.ails.backend.security;

/**
 * 认证通过后放入 SecurityContext 的 principal 对象。
 * <p>
 * {@code token} 是本次请求携带的<b>原始 JWT</b>（已去掉 "Bearer " 前缀），原样存下来是为了能透传：
 * ChatService 组装 {@code ChatAgentRequest} 时要把它交给 Python Agent，
 * Agent 回访后端那四个只读接口（concept / understanding / memory / evidence）时
 * 用的就是用户本人的身份。没有它，Agent 侧 {@code retriever_mode=auto} 会永远降级到 Fixture，
 * 等于「检索线看起来接好了、其实一次都没跑」。
 * <p>
 * 存 token 不等于有状态：它不落库、不缓存，只活在一次请求的 SecurityContext 里，
 * 请求结束即消失，与 {@code SessionCreationPolicy.STATELESS} 不冲突。
 */
public record AuthUser(Long id, String username, String token) {
}
