package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import com.ails.backend.model.MemoryCandidateDraft;
import com.baomidou.mybatisplus.extension.plugins.MybatisPlusInterceptor;
import com.baomidou.mybatisplus.extension.plugins.inner.OptimisticLockerInnerInterceptor;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Memory 状态机专项：T3-T21（T1/T2/T22 在 MemoryControllerTest 中走 HTTP 层）。
 *
 * <p>并发用例用确定性方式模拟乐观锁的两个结果（1 / 0），并显式断言
 * {@code OptimisticLockerInnerInterceptor} 已注册，证明冲突不是静默成功。</p>
 */
@ExtendWith(MockitoExtension.class)
class MemoryStateMachineTest {

    @Mock
    private MemoryMapper memoryMapper;
    @Mock
    private MemorySourceMapper memorySourceMapper;
    @Mock
    private LearningSpaceService spaceService;
    @Mock
    private ConceptService conceptService;

    private MemoryService service;

    @BeforeEach
    void setUp() {
        service = new MemoryService(memoryMapper, memorySourceMapper, spaceService, conceptService);
        lenient().when(memoryMapper.selectCount(any())).thenReturn(0L);
        lenient().when(memoryMapper.updateById(any(Memory.class))).thenReturn(1);
        lenient().when(memorySourceMapper.selectCount(any())).thenReturn(0L);
    }

    // ==================== T3-T5 合法转换 ====================

    @Test
    @DisplayName("T3 CANDIDATE → ACTIVE：用户确认成功")
    void t3_candidateToActiveSucceeds() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.CANDIDATE));

        MemoryResponse response = service.update(1L, 42L, request(MemoryStatus.ACTIVE));

        assertThat(response.status()).isEqualTo(MemoryStatus.ACTIVE);
        assertThat(capturedUpdated().getStatus()).isEqualTo(MemoryStatus.ACTIVE);
    }

    @Test
    @DisplayName("T4 CANDIDATE → ARCHIVED：用户拒绝候选，DELETE 成功")
    void t4_candidateToArchivedByDeleteSucceeds() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.CANDIDATE));

        service.archive(1L, 42L);

        assertThat(capturedUpdated().getStatus()).isEqualTo(MemoryStatus.ARCHIVED);
    }

    @Test
    @DisplayName("T5 ACTIVE → ARCHIVED：生效记忆归档成功")
    void t5_activeToArchivedByDeleteSucceeds() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.ACTIVE));

        service.archive(1L, 42L);

        assertThat(capturedUpdated().getStatus()).isEqualTo(MemoryStatus.ARCHIVED);
    }

    // ==================== T6-T8 非法转换 / 归档整体拒绝 ====================

    @Test
    @DisplayName("T6 ARCHIVED → CANDIDATE：409 且不写库")
    void t6_archivedToCandidateRejected() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.ARCHIVED));

        assertConflict(() -> service.update(1L, 42L, request(MemoryStatus.CANDIDATE)),
                "ARCHIVED → CANDIDATE");
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T7 ACTIVE → CANDIDATE：409 且不写库")
    void t7_activeToCandidateRejected() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.ACTIVE));

        assertConflict(() -> service.update(1L, 42L, request(MemoryStatus.CANDIDATE)),
                "ACTIVE → CANDIDATE");
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T8 ARCHIVED 记忆编辑内容：整体拒绝 409 且不写库")
    void t8_archivedContentEditRejected() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.ARCHIVED));

        assertConflict(() -> service.update(1L, 42L, request(null)), "已归档");
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    // ==================== T9-T11 不存在 / 跨用户 ====================

    @Test
    @DisplayName("T9 不存在的 Memory：update / archive 均为 404")
    void t9_missingMemoryReturns404() {
        when(memoryMapper.selectById(404L)).thenReturn(null);

        assertNotFound(() -> service.update(1L, 404L, request(MemoryStatus.ACTIVE)));
        assertNotFound(() -> service.archive(1L, 404L));
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T10 跨用户更新：404 且不写库")
    void t10_crossUserUpdateReturns404() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 2L, MemoryStatus.ACTIVE));

        assertNotFound(() -> service.update(1L, 42L, request(MemoryStatus.ARCHIVED)));
        assertNotFound(() -> service.archive(1L, 42L));
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T11 跨用户挂载空间：404 且不写库")
    void t11_crossUserSpaceReturns404() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.ACTIVE));
        when(spaceService.ownedSpace(1L, 99L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertNotFound(() -> service.update(1L, 42L,
                new MemoryRequest(99L, null, MemoryType.LEARNING, "k", "new", 0.6f, null)));
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    // ==================== T12-T13 重复请求 ====================

    @Test
    @DisplayName("T12 重复 PATCH 同状态：幂等成功，最终仍 ACTIVE")
    void t12_repeatSameStatePatchIsIdempotent() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, 1L, MemoryStatus.ACTIVE));

        assertThat(service.update(1L, 42L, request(MemoryStatus.ACTIVE)).status())
                .isEqualTo(MemoryStatus.ACTIVE);
        assertThat(service.update(1L, 42L, request(MemoryStatus.ACTIVE)).status())
                .isEqualTo(MemoryStatus.ACTIVE);
        verify(memoryMapper, times(2)).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T13 重复 DELETE：第二次无写入，最终 ARCHIVED")
    void t13_repeatDeleteIsIdempotent() {
        when(memoryMapper.selectById(42L))
                .thenReturn(memory(42L, 1L, MemoryStatus.ACTIVE))
                .thenReturn(memory(42L, 1L, MemoryStatus.ARCHIVED));

        service.archive(1L, 42L);
        service.archive(1L, 42L);

        verify(memoryMapper, times(1)).updateById(any(Memory.class));
    }

    // ==================== T14-T15 并发冲突 ====================

    @Test
    @DisplayName("T14 并发确认 / 归档：恰好一个成功，一个 409")
    void t14_concurrentConfirmAndArchiveDoNotSilentlyOverwrite() throws Exception {
        assertOptimisticLockerRegistered();

        CountDownLatch readBarrier = new CountDownLatch(2);
        when(memoryMapper.selectById(42L)).thenAnswer(invocation -> {
            readBarrier.countDown();
            assertThat(readBarrier.await(2, TimeUnit.SECONDS)).isTrue();
            return memory(42L, 1L, MemoryStatus.CANDIDATE);
        });
        AtomicInteger attempts = new AtomicInteger();
        AtomicReference<MemoryStatus> committed = new AtomicReference<>();
        when(memoryMapper.updateById(any(Memory.class))).thenAnswer(invocation -> {
            Memory memory = invocation.getArgument(0);
            if (attempts.incrementAndGet() == 1) {
                committed.set(memory.getStatus());
                return 1;
            }
            return 0;
        });

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            List<Throwable> failures = runConcurrently(pool,
                    () -> service.update(1L, 42L, request(MemoryStatus.ACTIVE)),
                    () -> {
                        service.archive(1L, 42L);
                        return null;
                    });

            assertThat(failures).hasSize(1);
            assertThat(failures.get(0)).isInstanceOf(BusinessException.class);
            assertThat(((BusinessException) failures.get(0)).getCode()).isEqualTo(409);
            assertThat(committed.get()).isIn(MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED);
            assertThat(attempts.get()).isEqualTo(2);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    @DisplayName("T15 并发两次确认：一个成功，一个 409，不会回到 CANDIDATE")
    void t15_concurrentDoubleConfirmKeepsActive() throws Exception {
        assertOptimisticLockerRegistered();

        CountDownLatch readBarrier = new CountDownLatch(2);
        when(memoryMapper.selectById(42L)).thenAnswer(invocation -> {
            readBarrier.countDown();
            assertThat(readBarrier.await(2, TimeUnit.SECONDS)).isTrue();
            return memory(42L, 1L, MemoryStatus.CANDIDATE);
        });
        AtomicInteger attempts = new AtomicInteger();
        AtomicReference<MemoryStatus> committed = new AtomicReference<>();
        when(memoryMapper.updateById(any(Memory.class))).thenAnswer(invocation -> {
            Memory memory = invocation.getArgument(0);
            if (attempts.incrementAndGet() == 1) {
                committed.set(memory.getStatus());
                return 1;
            }
            return 0;
        });

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            List<Throwable> failures = runConcurrently(pool,
                    () -> service.update(1L, 42L, request(MemoryStatus.ACTIVE)),
                    () -> service.update(1L, 42L, request(MemoryStatus.ACTIVE)));

            assertThat(failures).hasSize(1);
            assertThat(((BusinessException) failures.get(0)).getCode()).isEqualTo(409);
            assertThat(committed.get()).isEqualTo(MemoryStatus.ACTIVE);
        } finally {
            pool.shutdownNow();
        }
    }

    // ==================== T16-T21 Agent 路径 ====================

    @Test
    @DisplayName("T16 Agent ADD：只落 CANDIDATE")
    void t16_agentAddPersistsCandidate() {
        when(memoryMapper.selectOne(any())).thenReturn(null);
        when(memoryMapper.insert(any(Memory.class))).thenAnswer(invocation -> {
            invocation.<Memory>getArgument(0).setId(55L);
            return 1;
        });

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(MemoryReconcileAction.ADD, "agent fact"), MemorySourceType.MESSAGE, 100L);

        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.ADDED);
        assertThat(capturedInserted().getStatus()).isEqualTo(MemoryStatus.CANDIDATE);
    }

    @Test
    @DisplayName("T17 Agent UPDATE 候选：内容更新，状态仍 CANDIDATE")
    void t17_agentUpdateCandidateKeepsCandidate() {
        when(memoryMapper.selectOne(any())).thenReturn(memory(42L, 1L, MemoryStatus.CANDIDATE));

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(MemoryReconcileAction.UPDATE, "agent corrected"), MemorySourceType.MESSAGE, 100L);

        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.UPDATED);
        Memory updated = capturedUpdated();
        assertThat(updated.getStatus()).isEqualTo(MemoryStatus.CANDIDATE);
        assertThat(updated.getContent()).isEqualTo("agent corrected");
    }

    @Test
    @DisplayName("T18 Agent UPDATE 生效记忆：SKIPPED，不覆盖")
    void t18_agentUpdateActiveIsSkipped() {
        when(memoryMapper.selectOne(any())).thenReturn(memory(42L, 1L, MemoryStatus.ACTIVE));

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(MemoryReconcileAction.UPDATE, "agent overwrite"), MemorySourceType.MESSAGE, 100L);

        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.SKIPPED);
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T19 Agent UPDATE 归档记忆：SKIPPED，不复活")
    void t19_agentUpdateArchivedIsSkipped() {
        when(memoryMapper.selectOne(any())).thenReturn(memory(42L, 1L, MemoryStatus.ARCHIVED));

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(MemoryReconcileAction.UPDATE, "agent overwrite"), MemorySourceType.MESSAGE, 100L);

        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.SKIPPED);
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("T20 createCandidate 忽略请求 status，固定落 CANDIDATE")
    void t20_createCandidateIgnoresRequestedActive() {
        when(memoryMapper.insert(any(Memory.class))).thenAnswer(invocation -> {
            invocation.<Memory>getArgument(0).setId(56L);
            return 1;
        });

        Memory memory = service.createCandidate(1L,
                new MemoryRequest(null, null, MemoryType.LEARNING, "k", "agent fact", 0.6f, MemoryStatus.ACTIVE),
                MemorySourceType.MESSAGE, 100L);

        assertThat(memory.getStatus()).isEqualTo(MemoryStatus.CANDIDATE);
        assertThat(capturedInserted().getStatus()).isEqualTo(MemoryStatus.CANDIDATE);
    }

    @Test
    @DisplayName("T21 Agent 更新遇到用户已确认：版本冲突 -> SKIPPED，不抛异常")
    void t21_agentUpdateLosesOptimisticLockRace() {
        when(memoryMapper.selectOne(any())).thenReturn(memory(42L, 1L, MemoryStatus.CANDIDATE));
        when(memoryMapper.updateById(any(Memory.class))).thenReturn(0);

        MemoryService.ReconcileOutcome outcome = service.reconcileCandidate(
                1L, draft(MemoryReconcileAction.UPDATE, "agent overwrite"), MemorySourceType.MESSAGE, 100L);

        assertThat(outcome).isEqualTo(MemoryService.ReconcileOutcome.SKIPPED);
        verify(memoryMapper).updateById(any(Memory.class));
    }

    // ==================== helpers ====================

    private static Memory memory(long id, long userId, MemoryStatus status) {
        Memory memory = new Memory();
        memory.setId(id);
        memory.setUserId(userId);
        memory.setScopeType(MemoryScopeType.GLOBAL);
        memory.setScopeKey("GLOBAL");
        memory.setMemoryKey("k");
        memory.setMemoryType(MemoryType.LEARNING);
        memory.setContent("old");
        memory.setImportance(0.5f);
        memory.setStatus(status);
        memory.setVersion(0);
        return memory;
    }

    private static MemoryRequest request(MemoryStatus status) {
        return new MemoryRequest(null, null, MemoryType.LEARNING, "k", "new", 0.6f, status);
    }

    private static MemoryCandidateDraft draft(MemoryReconcileAction action, String content) {
        return new MemoryCandidateDraft(MemoryScopeType.GLOBAL, null, null, MemoryType.LEARNING,
                "k", content, 0.6f, action);
    }

    private Memory capturedUpdated() {
        ArgumentCaptor<Memory> captor = ArgumentCaptor.forClass(Memory.class);
        verify(memoryMapper).updateById(captor.capture());
        return captor.getValue();
    }

    private Memory capturedInserted() {
        ArgumentCaptor<Memory> captor = ArgumentCaptor.forClass(Memory.class);
        verify(memoryMapper).insert(captor.capture());
        return captor.getValue();
    }

    private static void assertConflict(Runnable action, String messagePart) {
        assertThatThrownBy(action::run)
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(409);
                    assertThat(ex.getMessage()).contains(messagePart);
                });
    }

    private static void assertNotFound(Runnable action) {
        assertThatThrownBy(action::run)
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));
    }

    private static void assertOptimisticLockerRegistered() {
        MybatisPlusInterceptor interceptor = new MybatisPlusConfig().mybatisPlusInterceptor();
        assertThat(interceptor.getInterceptors())
                .anyMatch(inner -> inner instanceof OptimisticLockerInnerInterceptor);
    }

    @SafeVarargs
    private static List<Throwable> runConcurrently(ExecutorService pool, ThrowingTask... tasks)
            throws InterruptedException, ExecutionException, TimeoutException {
        CountDownLatch start = new CountDownLatch(1);
        List<Future<?>> futures = new ArrayList<>();
        for (ThrowingTask task : tasks) {
            futures.add(pool.submit(() -> {
                start.await();
                return task.run();
            }));
        }
        start.countDown();

        List<Throwable> failures = new ArrayList<>();
        for (Future<?> future : futures) {
            try {
                future.get(5, TimeUnit.SECONDS);
            } catch (ExecutionException e) {
                failures.add(e.getCause());
            }
        }
        return failures;
    }

    @FunctionalInterface
    private interface ThrowingTask {
        Object run() throws Exception;
    }
}
