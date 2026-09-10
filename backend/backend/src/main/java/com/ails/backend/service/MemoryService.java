package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.entity.Memory;
import com.ails.backend.entity.MemorySource;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.Objects;

/**
 * Agent 长期记忆的生命周期管理。
 * <p>
 * Memory 与 Understanding 是两回事：Understanding 是「用户对知识点的认知模型」，
 * Memory 是「Agent 将来服务这个用户时要带着的长期上下文」（他的偏好、学习方式、
 * 反复出现的困惑）。
 * <p>
 * <b>生命周期用 status 管理，不做物理删除：</b>
 * <pre>
 *   CANDIDATE（Agent 推断，待确认） → ACTIVE（生效） → ARCHIVED（归档）
 * </pre>
 * 所以对外那个 {@code DELETE /api/memories/{id}} 在语义上是「归档」，不是删除 ——
 * 并且它不触碰 Evidence 与 Understanding，学习历史不受影响。
 */
@Service
@RequiredArgsConstructor
public class MemoryService {

    private final MemoryMapper memoryMapper;
    private final MemorySourceMapper memorySourceMapper;

    /** 用户自己创建的 Memory：直接生效，不需要确认 */
    @Transactional
    public MemoryResponse create(Long userId, MemoryRequest request) {
        return MemoryResponse.from(persist(userId, request, MemoryStatus.ACTIVE));
    }

    /**
     * 记录一条 Agent 推断出的 Memory。
     * <p>
     * 这是留给 Agent Runtime 的入口：AI 推断出的内容默认只能停在 CANDIDATE，
     * 必须由用户确认（{@link #update} 里改 status）才升到 ACTIVE。
     * 当前还没有 Agent 往这里写，所以线上不会有 CANDIDATE 数据。
     */
    @Transactional
    public Memory createCandidate(Long userId, MemoryRequest request,
                                  MemorySourceType sourceType, Long sourceId) {
        Memory memory = persist(userId, request, MemoryStatus.CANDIDATE);
        MemorySource source = new MemorySource();
        source.setMemoryId(memory.getId());
        source.setSourceType(sourceType);
        source.setSourceId(sourceId);
        memorySourceMapper.insert(source);
        return memory;
    }

    @Transactional(readOnly = true)
    public List<MemoryResponse> list(Long userId, Long spaceId, Long conceptId, MemoryStatus status) {
        return memoryMapper.selectList(new LambdaQueryWrapper<Memory>()
                        .eq(Memory::getUserId, userId)
                        .eq(spaceId != null, Memory::getLearningSpaceId, spaceId)
                        .eq(conceptId != null, Memory::getConceptId, conceptId)
                        .eq(status != null, Memory::getStatus, status)
                        .orderByDesc(Memory::getImportance)
                        .orderByDesc(Memory::getUpdatedAt))
                .stream().map(MemoryResponse::from).toList();
    }

    /**
     * 记忆检索（第一版为关键词匹配；接向量库后由 embeddingRef 承担）。
     */
    @Transactional(readOnly = true)
    public List<MemoryResponse> search(Long userId, Long spaceId, String keyword) {
        return memoryMapper.selectList(new LambdaQueryWrapper<Memory>()
                        .eq(Memory::getUserId, userId)
                        .eq(Memory::getStatus, MemoryStatus.ACTIVE)
                        .eq(spaceId != null, Memory::getLearningSpaceId, spaceId)
                        .like(StringUtils.hasText(keyword), Memory::getContent, keyword)
                        .orderByDesc(Memory::getImportance)
                        .orderByDesc(Memory::getUpdatedAt))
                .stream().map(MemoryResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public MemoryResponse get(Long userId, Long memoryId) {
        return MemoryResponse.from(ownedMemory(userId, memoryId));
    }

    @Transactional
    public MemoryResponse update(Long userId, Long memoryId, MemoryRequest request) {
        Memory memory = ownedMemory(userId, memoryId);
        memory.setMemoryType(request.memoryType());
        memory.setPath(request.path());
        memory.setContent(request.content().trim());
        memory.setImportance(request.importance());
        if (request.learningSpaceId() != null) {
            memory.setLearningSpaceId(request.learningSpaceId());
        }
        if (request.conceptId() != null) {
            memory.setConceptId(request.conceptId());
        }
        if (request.status() != null) {
            memory.setStatus(request.status());
        }
        memoryMapper.updateById(memory);
        return MemoryResponse.from(memory);
    }

    /**
     * 归档。对外表现为 {@code DELETE /api/memories/{id}}，但内部只改状态 ——
     * Evidence / Understanding 完全不受影响。
     */
    @Transactional
    public void archive(Long userId, Long memoryId) {
        Memory memory = ownedMemory(userId, memoryId);
        memory.setStatus(MemoryStatus.ARCHIVED);
        memoryMapper.updateById(memory);
    }

    // ================= 内部工具 =================

    private Memory persist(Long userId, MemoryRequest request, MemoryStatus status) {
        Memory memory = new Memory();
        memory.setUserId(userId);
        memory.setLearningSpaceId(request.learningSpaceId());
        memory.setConceptId(request.conceptId());
        memory.setMemoryType(request.memoryType() == null ? MemoryType.LEARNING : request.memoryType());
        memory.setPath(request.path());
        memory.setContent(request.content().trim());
        memory.setStatus(status);
        memory.setImportance(request.importance());
        memoryMapper.insert(memory);
        return memory;
    }

    private Memory ownedMemory(Long userId, Long memoryId) {
        Memory memory = memoryMapper.selectById(memoryId);
        if (memory == null || !Objects.equals(memory.getUserId(), userId)) {
            throw BusinessException.notFound("记忆不存在");
        }
        return memory;
    }
}
