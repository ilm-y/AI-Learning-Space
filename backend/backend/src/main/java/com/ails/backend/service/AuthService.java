package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.auth.AuthResponse;
import com.ails.backend.dto.auth.LoginRequest;
import com.ails.backend.dto.auth.RegisterRequest;
import com.ails.backend.dto.user.UserDTO;
import com.ails.backend.entity.User;
import com.ails.backend.mapper.UserMapper;
import com.ails.backend.utils.JwtUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

/**
 * 注册 / 登录 / JWT 签发。
 */
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserMapper userMapper;
    private final PasswordEncoder passwordEncoder;
    private final JwtUtil jwtUtil;

    @Transactional
    public AuthResponse register(RegisterRequest request) {
        Long usernameCount = userMapper.selectCount(
                new LambdaQueryWrapper<User>().eq(User::getUsername, request.username()));
        if (usernameCount != null && usernameCount > 0) {
            throw BusinessException.conflict("用户名已存在");
        }
        String email = StringUtils.hasText(request.email()) ? request.email().trim() : null;
        if (email != null) {
            Long emailCount = userMapper.selectCount(
                    new LambdaQueryWrapper<User>().eq(User::getEmail, email));
            if (emailCount != null && emailCount > 0) {
                throw BusinessException.conflict("邮箱已被使用");
            }
        }

        User user = new User();
        user.setUsername(request.username().trim());
        user.setPasswordHash(passwordEncoder.encode(request.password()));
        user.setEmail(email);
        userMapper.insert(user);
        return issueToken(user);
    }

    @Transactional(readOnly = true)
    public AuthResponse login(LoginRequest request) {
        User user = userMapper.selectOne(
                new LambdaQueryWrapper<User>().eq(User::getUsername, request.username()));
        if (user == null || !passwordEncoder.matches(request.password(), user.getPasswordHash())) {
            throw new BusinessException(401, "用户名或密码错误");
        }
        return issueToken(user);
    }

    private AuthResponse issueToken(User user) {
        String token = jwtUtil.generateToken(user.getId(), user.getUsername());
        return new AuthResponse(token, UserDTO.from(user));
    }
}
