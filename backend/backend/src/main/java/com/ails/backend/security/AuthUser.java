package com.ails.backend.security;

/**
 * 认证通过后放入 SecurityContext 的 principal 对象。
 */
public record AuthUser(Long id, String username) {
}
