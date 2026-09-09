package com.ails.backend.dto.user;

import jakarta.validation.constraints.Email;

/**
 * 更新个人资料请求。
 */
public record UpdateProfileRequest(
        @Email String email
) {
}
