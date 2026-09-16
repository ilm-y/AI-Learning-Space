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

    /**
     * 删除空间 —— 实际是<b>软删</b>（@TableLogic 把 deleteById 变成 UPDATE deleted_at）。
     * <p>
     * 子表<b>不会</b>被级联清理，也不可能被清理：① 没有任何指向 learning_space 的外键带
     * ON DELETE CASCADE；② 软删走的是 UPDATE，外键动作根本不会触发。
     * 所以 conversation / concept / learning_evidence / learning_understanding 这些行原样留在库里
     * —— 这正是刻意为之：空间只是容器，删掉它不该毁掉学习历史。
     * <p>
     * 这些残留行「看不见」的口径统一由各自的归属校验负责：空间软删后，
     * {@code ConceptService.requireOwnedSpace} 与 {@code ConversationService.ownedConversation}
     * 都会因为它查不到而返回 404，因此从 API 上看不到、也写不进这个空间的任何数据。
     */
    @Transactional
    public void delete(Long userId, Long spaceId) {
        ownedSpace(userId, spaceId);
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
