package com.ails.backend.utils;

import com.ails.backend.common.BusinessException;
import com.ails.backend.security.AuthUser;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

/**
 * 当前登录用户上下文工具。仅在被 security 保护的接口中调用。
 */
public final class SecurityUtil {

    private SecurityUtil() {
    }

    public static AuthUser currentUser() {
        Authentication auth = SecurityContextHolder.getContext().getAuthentication();
        if (auth != null && auth.getPrincipal() instanceof AuthUser user) {
            return user;
        }
        throw BusinessException.unauthorized("未登录或登录已过期");
    }

    public static Long currentUserId() {
        return currentUser().id();
    }

    /**
     * 当前请求携带的原始 JWT（不含 "Bearer " 前缀）。
     * <p>
     * 用途只有一个：把它透传给下游服务，让下游以<b>同一个用户</b>的身份回调后端的鉴权接口。
     * 目前是 ChatService 组装 {@code ChatAgentRequest} 时塞给 Python Agent。
     */
    public static String currentToken() {
        return currentUser().token();
    }
}
