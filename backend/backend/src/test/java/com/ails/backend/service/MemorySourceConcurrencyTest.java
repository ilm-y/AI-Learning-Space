package com.ails.backend.service;

import com.ails.backend.entity.Memory;
import com.ails.backend.entity.MemorySource;
import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import com.ails.backend.model.MemoryCandidateDraft;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DuplicateKeyException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.when;

/**
 * Memory 补挂来源的唯一键冲突同样不能回滚整份分析。
 */
@ExtendWith(MockitoExtension.class)
class MemorySourceConcurrencyTest {

    @Mock
    private MemoryMapper memoryMapper;
    @Mock
    private MemorySourceMapper memorySourceMapper;
    @Mock
    private LearningSpaceService spaceService;
    @Mock
    private ConceptService conceptService;
    @Mock
    private MemoryOutboxPublisher outboxPublisher;
    @Mock
    private HybridSearchService hybridSearchService;

    private MemoryService newService() {
        return new MemoryService(memoryMapper, memorySourceMapper, spaceService,
                conceptService, outboxPublisher, hybridSearchService);
    }

    @Test
    void duplicateSourceFromConcurrentRunStillReportsAdded() {
        MemoryService service = newService();

        when(memoryMapper.selectOne(any())).thenReturn(null);
        doAnswer(invocation -> {
            Memory memory = invocation.getArgument(0);
            memory.setId(55L);
            return 1;
        }).when(memoryMapper).insert(any(Memory.class));
        // 第一次查：来源还不存在；插入撞唯一键后再查：并发事务已经把它插进去了
        when(memorySourceMapper.selectCount(any())).thenReturn(0L, 1L);
        when(memorySourceMapper.insert(any(MemorySource.class)))
                .thenThrow(new DuplicateKeyException("dup"));

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(), MemorySourceType.MESSAGE, 100L);

        // 记忆本身已写入，来源冲突不是失败：整份分析不应因此回滚
        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.ADDED);
    }

    @Test
    void duplicateSourceFromAnotherConstraintIsNotSwallowed() {
        MemoryService service = newService();

        when(memoryMapper.selectOne(any())).thenReturn(null);
        doAnswer(invocation -> {
            Memory memory = invocation.getArgument(0);
            memory.setId(55L);
            return 1;
        }).when(memoryMapper).insert(any(Memory.class));
        when(memorySourceMapper.selectCount(any())).thenReturn(0L);
        when(memorySourceMapper.insert(any(MemorySource.class)))
                .thenThrow(new DuplicateKeyException("foreign key"));

        assertThatThrownBy(() -> service.reconcileCandidate(
                1L, draft(), MemorySourceType.MESSAGE, 100L))
                .isInstanceOf(DuplicateKeyException.class);
    }

    @Test
    void concurrentInsertOfSameMemoryKeyIsTreatedAsExisting() {
        MemoryService service = newService();

        // 第一次按 (userId, scopeKey, memoryKey) 查不到；insert 撞唯一键；
        // 再查能拿到并发事务插入的那一行 —— 这才是可以安全吞掉的冲突。
        Memory concurrent = new Memory();
        concurrent.setId(55L);
        when(memoryMapper.selectOne(any())).thenReturn(null, concurrent);
        when(memoryMapper.insert(any(Memory.class)))
                .thenThrow(new DuplicateKeyException("dup"));

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(), MemorySourceType.MESSAGE, 100L);

        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.SKIPPED);
    }

    private static MemoryCandidateDraft draft() {
        return new MemoryCandidateDraft(
                MemoryScopeType.GLOBAL, null, null, MemoryType.LEARNING,
                "prefers-examples", "偏好用具体例子解释", 0.6f,
                MemoryReconcileAction.ADD);
    }
}
