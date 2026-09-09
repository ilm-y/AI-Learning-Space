package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.user.ChangePasswordRequest;
import com.ails.backend.dto.user.UpdateProfileRequest;
import com.ails.backend.dto.user.UserDTO;
import com.ails.backend.entity.User;
import com.ails.backend.mapper.UserMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

/**
 * 用户资料维护。
 */
@Service
@RequiredArgsConstructor
public class UserService {

    private final UserMapper userMapper;
    private final PasswordEncoder passwordEncoder;

    @Transactional(readOnly = true)
    public UserDTO me(Long userId) {
        return UserDTO.from(requireUser(userId));
    }

    @Transactional
    public UserDTO updateProfile(Long userId, UpdateProfileRequest request) {
        User user = requireUser(userId);
        String newEmail = request.email() == null ? null : request.email().trim();
        if (StringUtils.hasText(newEmail) && !newEmail.equals(user.getEmail())) {
            Long count = userMapper.selectCount(
                    new LambdaQueryWrapper<User>()
                            .eq(User::getEmail, newEmail)
                            .ne(User::getId, userId));
            if (count != null && count > 0) {
                throw BusinessException.conflict("邮箱已被使用");
            }
            user.setEmail(newEmail);
            userMapper.updateById(user);
        }
        return UserDTO.from(requireUser(userId));
    }

    @Transactional
    public void changePassword(Long userId, ChangePasswordRequest request) {
        User user = requireUser(userId);
        if (!passwordEncoder.matches(request.oldPassword(), user.getPasswordHash())) {
            throw new BusinessException(400, "旧密码错误");
        }
        user.setPasswordHash(passwordEncoder.encode(request.newPassword()));
        userMapper.updateById(user);
    }

    private User requireUser(Long userId) {
        User user = userMapper.selectById(userId);
        if (user == null) {
            throw BusinessException.notFound("用户不存在");
        }
        return user;
    }
}
