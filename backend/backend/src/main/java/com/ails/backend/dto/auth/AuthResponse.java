package com.ails.backend.dto.auth;

import com.ails.backend.dto.user.UserDTO;

/**
 * 登录/注册成功响应：JWT + 用户信息。
 */
public record AuthResponse(
        String token,
        String tokenType,
        UserDTO user
) {
    public AuthResponse(String token, UserDTO user) {
        this(token, "Bearer", user);
    }
}
