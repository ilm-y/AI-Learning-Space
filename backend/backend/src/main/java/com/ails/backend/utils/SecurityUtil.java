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
}
