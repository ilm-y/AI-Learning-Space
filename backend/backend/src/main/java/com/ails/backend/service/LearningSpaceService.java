package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.space.LearningSpaceRequest;
import com.ails.backend.dto.space.LearningSpaceResponse;
import com.ails.backend.entity.LearningSpace;
import com.ails.backend.enums.SpaceVisibility;
import com.ails.backend.mapper.LearningSpaceMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;

/**
 * 学习空间 CRUD（归属校验：资源必须属于当前用户）。
 */
@Service
@RequiredArgsConstructor
public class LearningSpaceService {

    private final LearningSpaceMapper spaceMapper;

    @Transactional(readOnly = true)
    public List<LearningSpaceResponse> listMine(Long userId) {
        return spaceMapper.selectList(new LambdaQueryWrapper<LearningSpace>()
                        .eq(LearningSpace::getUserId, userId)
                        .orderByDesc(LearningSpace::getCreatedAt))
                .stream().map(LearningSpaceResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public LearningSpaceResponse get(Long userId, Long spaceId) {
        return LearningSpaceResponse.from(ownedSpace(userId, spaceId));
    }

    @Transactional
    public LearningSpaceResponse create(Long userId, LearningSpaceRequest request) {
        LearningSpace space = new LearningSpace();
        space.setUserId(userId);
        space.setName(request.name().trim());
        space.setDescription(request.description());
        space.setVisibility(request.visibility() == null ? SpaceVisibility.PRIVATE : request.visibility());
        spaceMapper.insert(space);
        return LearningSpaceResponse.from(space);
    }

    @Transactional
    public LearningSpaceResponse update(Long userId, Long spaceId, LearningSpaceRequest request) {
        LearningSpace space = ownedSpace(userId, spaceId);
        if (StringUtils.hasText(request.name())) {
            space.setName(request.name().trim());
        }
        space.setDescription(request.description());
        if (request.visibility() != null) {
            space.setVisibility(request.visibility());
        }
        spaceMapper.updateById(space);
        return LearningSpaceResponse.from(space);
    }

    @Transactional
    public void delete(Long userId, Long spaceId) {
        ownedSpace(userId, spaceId);
        // 子表均建了 ON DELETE CASCADE 外键，删除空间会级联清理
        spaceMapper.deleteById(spaceId);
    }

    /**
     * 取当前用户拥有的空间，非本人资源一律视为不存在（404）。
     */
    public LearningSpace ownedSpace(Long userId, Long spaceId) {
        LearningSpace space = spaceMapper.selectById(spaceId);
        if (space == null || !space.getUserId().equals(userId)) {
            throw BusinessException.notFound("学习空间不存在");
        }
        return space;
    }
}
