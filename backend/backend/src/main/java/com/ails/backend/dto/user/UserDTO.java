package com.ails.backend.dto.user;

import com.ails.backend.entity.User;

import java.time.LocalDateTime;

/**
 * 用户响应体（绝不包含 passwordHash）。
 */
public record UserDTO(
        Long id,
        String username,
        String email,
        LocalDateTime createdAt
) {
    public static UserDTO from(User user) {
        if (user == null) {
            return null;
        }
        return new UserDTO(user.getId(), user.getUsername(), user.getEmail(), user.getCreatedAt());
    }
}
