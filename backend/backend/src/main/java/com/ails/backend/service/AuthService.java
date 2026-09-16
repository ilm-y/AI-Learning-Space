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
 * <p>
 * <b>用户名在进入本类时就先 trim 一次，之后全程只用这一份。</b>
 * 注册时「查重」与「入库」如果用了两个不同的字符串，{@code " alice"} 能绕过 {@code "alice"}
 * 占用的那一行（查重查的是没 trim 的、库里存的是 trim 过的，命中不了），最后反而在
 * {@code uk_user_username} 上撞成 500；登录同理，用户在输入框里多敲一个空格就登不进来。
 * 所以次数只有一次、位置只有一处，见下面两处 {@code username} 局部变量。
 */
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserMapper userMapper;
    private final PasswordEncoder passwordEncoder;
    private final JwtUtil jwtUtil;

    @Transactional
    public AuthResponse register(RegisterRequest request) {
        // 唯一权威的用户名形态：查重、入库、返回都用它，见类注释
        String username = request.username().trim();
        Long usernameCount = userMapper.selectCount(
                new LambdaQueryWrapper<User>().eq(User::getUsername, username));
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
        user.setUsername(username);
        user.setPasswordHash(passwordEncoder.encode(request.password()));
        user.setEmail(email);
        userMapper.insert(user);
        return issueToken(user);
    }

    @Transactional(readOnly = true)
    public AuthResponse login(LoginRequest request) {
        // 与注册用同一套规范化：库里的用户名一定是 trim 过的，这里也必须 trim 才查得到
        String username = request.username().trim();
        User user = userMapper.selectOne(
                new LambdaQueryWrapper<User>().eq(User::getUsername, username));
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
