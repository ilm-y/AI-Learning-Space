package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.concept.ConceptRequest;
import com.ails.backend.dto.concept.ConceptResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.LearningSpace;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.LearningSpaceMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;

/**
 * 知识点 CRUD：知识点挂在学习空间下，仅空间所有者可维护。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ConceptService {

    private final ConceptMapper conceptMapper;
    private final LearningSpaceMapper spaceMapper;

    @Transactional(readOnly = true)
    public List<ConceptResponse> listBySpace(Long userId, Long spaceId) {
        requireOwnedSpace(userId, spaceId);
        return conceptMapper.selectList(new LambdaQueryWrapper<Concept>()
                        .eq(Concept::getLearningSpaceId, spaceId)
                        .orderByAsc(Concept::getId))
                .stream().map(ConceptResponse::from).toList();
    }

    /**
     * 在学习空间下新建知识点。
     * <p>
     * 重名不能只靠数据库报错：{@code uk_concept_space_name(learning_space_id, canonical_name)}
     * 是普通唯一索引，<b>把软删的行也算在内</b>，而软删之后这个 API 依然允许用户用同一个名字
     * 再建一次。所以这里查的是<b>含软删行</b>的那一条（{@code selectAnyBySpaceAndName}），
     * 然后分三种情况处理，而不是把 DuplicateKeyException 直接漏成 500：
     * <ul>
     *   <li>同名且还活着 → 409，明确告诉用户重名，而不是 500；</li>
     *   <li>同名但已软删 → <b>复活那一行</b>（见下面注释说明为什么不是新插一行）；</li>
     *   <li>没有同名 → 正常插入。</li>
     * </ul>
     */
    @Transactional
    public ConceptResponse createInSpace(Long userId, Long spaceId, ConceptRequest request) {
        requireOwnedSpace(userId, spaceId);
        String canonicalName = request.canonicalName().trim();

        Concept existing = conceptMapper.selectAnyBySpaceAndName(spaceId, canonicalName);
        if (existing != null && existing.getDeletedAt() == null) {
            throw BusinessException.conflict("该学习空间下已存在同名知识点：" + canonicalName);
        }

        Concept concept;
        if (existing != null) {
            // 复活而不是新插一行：id 不变，挂在它上面的 LearningEvidence / LearningUnderstanding /
            // LearningState 就都跟着回到视野里。新插一行会让同一个知识点在历史里裂成两个
            // 互不相干的 id，用户之前学过的证据全部「失踪」。
            conceptMapper.revive(existing.getId());
            concept = existing;
            concept.setDeletedAt(null);
            // 复活场景下用户显然是来重新定义它的，所以字段一律用请求里的值覆盖
            concept.setDisplayName(StringUtils.hasText(request.displayName()) ? request.displayName().trim() : null);
            concept.setDescription(request.description());
            concept.setDifficultyLevel(request.difficultyLevel());
            conceptMapper.updateById(concept);
            return ConceptResponse.from(concept);
        }

        concept = new Concept();
        concept.setLearningSpaceId(spaceId);
        concept.setCanonicalName(canonicalName);
        concept.setDisplayName(StringUtils.hasText(request.displayName()) ? request.displayName().trim() : null);
        concept.setDescription(request.description());
        concept.setDifficultyLevel(request.difficultyLevel());
        try {
            conceptMapper.insert(concept);
        } catch (DuplicateKeyException e) {
            // 上面的检查与插入之间仍有窗口（并发建同名概念）。唯一键是最终裁判，
            // 把它翻译成与串行时一致的 409，而不是让它冒成 500。
            throw BusinessException.conflict("该学习空间下已存在同名知识点：" + canonicalName);
        }
        return ConceptResponse.from(concept);
    }

    @Transactional(readOnly = true)
    public ConceptResponse get(Long userId, Long conceptId) {
        return ConceptResponse.from(accessibleConcept(userId, conceptId));
    }

    @Transactional
    public ConceptResponse update(Long userId, Long conceptId, ConceptRequest request) {
        Concept concept = accessibleConcept(userId, conceptId);
        if (StringUtils.hasText(request.canonicalName())) {
            concept.setCanonicalName(request.canonicalName().trim());
        }
        concept.setDisplayName(StringUtils.hasText(request.displayName()) ? request.displayName().trim() : concept.getDisplayName());
        concept.setDescription(request.description());
        if (request.difficultyLevel() != null) {
            concept.setDifficultyLevel(request.difficultyLevel());
        }
        conceptMapper.updateById(concept);
        return ConceptResponse.from(concept);
    }

    /**
     * 软删知识点。
     * <p>
     * 不物理删除：concept 上挂着 LearningUnderstanding / LearningEvidence，删行会炸掉学习历史。
     * <p>
     * <b>软删之后数据仍在，但读不到了</b> —— 这一点容易记反。{@code uk_concept_space_name}
     * 仍占着这个名字（再建同名会走到 {@link #createInSpace} 的复活分支），而
     * Understanding / Evidence / LearningState 这些行本身没有任何删除入口、也不受影响，
     * 但<b>所有经过本类 {@code accessibleConcept} 的读接口</b>都会因为它已经查不到而返回 404。
     * 学习状态那边（overview / attention）则会把查不到概念的投影行直接滤掉，不展示空条目。
     * 想让它重新可见，唯一的办法是用同名 concept 再建一次 —— 走复活，id 不变，历史一并回来。
     */
    @Transactional
    public void delete(Long userId, Long conceptId) {
        Concept concept = accessibleConcept(userId, conceptId);
        conceptMapper.deleteById(concept.getId());
    }

    /**
     * 当前用户可访问的知识点：知识点的学习空间必须属于该用户。
     */
    public Concept accessibleConcept(Long userId, Long conceptId) {
        Concept concept = conceptMapper.selectById(conceptId);
        if (concept == null) {
            throw BusinessException.notFound("知识点不存在");
        }
        requireOwnedSpace(userId, concept.getLearningSpaceId());
        return concept;
    }

    private LearningSpace requireOwnedSpace(Long userId, Long spaceId) {
        LearningSpace space = spaceMapper.selectById(spaceId);
        if (space == null || !space.getUserId().equals(userId)) {
            throw BusinessException.notFound("学习空间不存在");
        }
        return space;
    }
}
