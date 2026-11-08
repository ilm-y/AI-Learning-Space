package com.ails.backend.service;

import com.ails.backend.config.DocumentReconciliationProperties;
import com.ails.backend.dto.document.ReconcilableDocument;
import com.ails.backend.dto.document.agent.AgentVectorCountResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.mapper.DocumentChunkMapper;
import com.ails.backend.mapper.DocumentMapper;
import com.ails.backend.mapper.ScheduledTaskLeaseMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * RAG-03：反向对账的状态语义与多实例互斥。
 *
 * <p>关键不变量：对账只写观测列，绝不修改 {@code document.status}；
 * “Qdrant 不可达”与“数量不一致”必须是不同状态。</p>
 */
@ExtendWith(MockitoExtension.class)
class DocumentVectorReconciliationServiceTest {

    private static final DocumentReconciliationProperties PROPERTIES =
            new DocumentReconciliationProperties(true, 3_600_000, 300_000, 200, 1_800_000);

    @Mock
    private DocumentMapper documentMapper;

    @Mock
    private DocumentChunkMapper chunkMapper;

    @Mock
    private DocumentIndexVersionService versionService;

    @Mock
    private AgentDocumentClient agentClient;

    @Mock
    private ScheduledTaskLeaseMapper leaseMapper;

    private DocumentVectorReconciliationService service;

    @BeforeEach
    void setUp() {
        service = new DocumentVectorReconciliationService(documentMapper, chunkMapper,
                versionService, agentClient, leaseMapper, PROPERTIES);
    }

    @Test
    void consistentCountsRecordOkAndNeverTouchDocumentStatus() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(7L, 2, "collection-x")));
        when(versionService.findCollection(7L, 2)).thenReturn("collection-x");
        when(chunkMapper.countActiveVectorRefs(7L, 2)).thenReturn(3);
        when(agentClient.countPoints(any(Document.class), eq("collection-x"), eq(2)))
                .thenReturn(new AgentVectorCountResponse("collection-x", true, 3));

        int mismatches = service.reconcileOnce();

        assertThat(mismatches).isZero();
        verify(documentMapper).updateVectorReconciliation(eq(7L), eq(2), eq("OK"), any(),
                detailContaining("expected=3", "actual=3", "collection=collection-x"));
        // 对账是观测行为：任何文档状态写操作都不允许发生。
        verify(documentMapper, never()).updateById(any(Document.class));
    }

    @Test
    void zeroPointsIsMismatchWithoutStatusCorruption() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(7L, 2, "collection-x")));
        when(versionService.findCollection(7L, 2)).thenReturn("collection-x");
        when(chunkMapper.countActiveVectorRefs(7L, 2)).thenReturn(3);
        when(agentClient.countPoints(any(Document.class), eq("collection-x"), eq(2)))
                .thenReturn(new AgentVectorCountResponse("collection-x", true, 0));

        int mismatches = service.reconcileOnce();

        assertThat(mismatches).isEqualTo(1);
        verify(documentMapper).updateVectorReconciliation(eq(7L), eq(2), eq("MISMATCH"), any(),
                detailContaining("expected=3", "actual=0"));
        verify(documentMapper, never()).updateById(any(Document.class));
    }

    @Test
    void partialPointLossReportsExactMismatchCounts() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(7L, 2, "collection-x")));
        when(versionService.findCollection(7L, 2)).thenReturn("collection-x");
        when(chunkMapper.countActiveVectorRefs(7L, 2)).thenReturn(5);
        when(agentClient.countPoints(any(Document.class), eq("collection-x"), eq(2)))
                .thenReturn(new AgentVectorCountResponse("collection-x", true, 4));

        assertThat(service.reconcileOnce()).isEqualTo(1);
        verify(documentMapper).updateVectorReconciliation(eq(7L), eq(2), eq("MISMATCH"), any(),
                detailContaining("expected=5", "actual=4"));
    }

    @Test
    void missingCollectionIsReachableMismatchNotUnreachable() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(7L, 2, "collection-x")));
        when(versionService.findCollection(7L, 2)).thenReturn("collection-x");
        when(chunkMapper.countActiveVectorRefs(7L, 2)).thenReturn(3);
        when(agentClient.countPoints(any(Document.class), eq("collection-x"), eq(2)))
                .thenReturn(new AgentVectorCountResponse("collection-x", false, 0));

        assertThat(service.reconcileOnce()).isEqualTo(1);
        verify(documentMapper).updateVectorReconciliation(eq(7L), eq(2), eq("MISMATCH"), any(),
                detailContaining("expected=3", "actual=0", "collection does not exist"));
        verify(documentMapper, never())
                .updateVectorReconciliation(anyLong(), anyInt(), eq("UNREACHABLE"), any(), anyString());
    }

    @Test
    void qdrantUnreachableIsDistinctAndDoesNotMarkDocumentAbnormal() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(7L, 2, "collection-x")));
        when(versionService.findCollection(7L, 2)).thenReturn("collection-x");
        when(chunkMapper.countActiveVectorRefs(7L, 2)).thenReturn(3);
        doThrow(new DocumentIndexException(503, "qdrant down"))
                .when(agentClient).countPoints(any(Document.class), eq("collection-x"), eq(2));

        int mismatches = service.reconcileOnce();

        // 不可达既不算 OK，也不算 MISMATCH：本轮不一致数为 0，且文档状态不变。
        assertThat(mismatches).isZero();
        verify(documentMapper).updateVectorReconciliation(eq(7L), eq(2), eq("UNREACHABLE"), any(),
                detailContaining("expected=3", "actual=<unknown>", "error="));
        verify(documentMapper, never())
                .updateVectorReconciliation(anyLong(), anyInt(), eq("MISMATCH"), any(), anyString());
        verify(documentMapper, never()).updateById(any(Document.class));
    }

    @Test
    void legacyDocumentWithoutCollectionIsUnlocatableAndNeverQueriesQdrant() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(8L, 1, null)));
        when(versionService.findCollection(8L, 1)).thenReturn(null);
        when(chunkMapper.countActiveVectorRefs(8L, 1)).thenReturn(2);

        int mismatches = service.reconcileOnce();

        assertThat(mismatches).isZero();
        verifyNoInteractions(agentClient);
        verify(documentMapper).updateVectorReconciliation(eq(8L), eq(1), eq("UNLOCATABLE"), any(),
                detailContaining("collection=<unlocatable>"));
    }

    @Test
    void fallsBackToDocumentCollectionWhenVersionRecordIsMissing() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(9L, 3, "legacy-recorded")));
        when(versionService.findCollection(9L, 3)).thenReturn(null);
        when(chunkMapper.countActiveVectorRefs(9L, 3)).thenReturn(1);
        when(agentClient.countPoints(any(Document.class), eq("legacy-recorded"), eq(3)))
                .thenReturn(new AgentVectorCountResponse("legacy-recorded", true, 1));

        assertThat(service.reconcileOnce()).isZero();
        verify(documentMapper).updateVectorReconciliation(eq(9L), eq(3), eq("OK"), any(),
                detailContaining("collection=legacy-recorded"));
    }

    @Test
    void skipsEntireRoundWhenAnotherInstanceHoldsTheLease() {
        when(leaseMapper.tryAcquire(eq(DocumentVectorReconciliationService.TASK_NAME),
                anyString(), any(), any())).thenReturn(0);

        assertThat(service.reconcileOnce()).isZero();

        verifyNoInteractions(documentMapper, chunkMapper, versionService, agentClient);
        verify(leaseMapper, never()).selectOwner(anyString());
        verify(leaseMapper, never()).release(anyString(), anyString());
    }

    @Test
    void secondInstanceCannotRunWhileFirstIsStillInsideTheRound() {
        InMemoryLeaseMapper leases = new InMemoryLeaseMapper();
        DocumentVectorReconciliationService first = serviceWith(leases);
        DocumentVectorReconciliationService second = serviceWith(leases);
        AtomicInteger scans = new AtomicInteger();

        when(documentMapper.selectReconcilableDocuments(200)).thenAnswer(invocation -> {
            if (scans.getAndIncrement() == 0) {
                // 第一实例尚未释放租约，第二实例同时触发：必须直接跳过。
                assertThat(second.reconcileOnce()).isZero();
            }
            return List.of();
        });

        assertThat(first.reconcileOnce()).isZero();
        assertThat(scans).hasValue(1);
        // 第一实例结束后主动释放，下一轮可再次执行。
        assertThat(first.reconcileOnce()).isZero();
    }

    @Test
    void observabilityWriteFailureDoesNotAbortReconciliation() {
        givenLease();
        when(documentMapper.selectReconcilableDocuments(200))
                .thenReturn(List.of(document(7L, 2, "collection-x")));
        when(versionService.findCollection(7L, 2)).thenReturn("collection-x");
        when(chunkMapper.countActiveVectorRefs(7L, 2)).thenReturn(1);
        when(agentClient.countPoints(any(Document.class), eq("collection-x"), eq(2)))
                .thenReturn(new AgentVectorCountResponse("collection-x", true, 2));
        when(documentMapper.updateVectorReconciliation(anyLong(), anyInt(), anyString(), any(),
                anyString())).thenThrow(new RuntimeException("mysql busy"));

        // 观测写失败只吞掉日志，不影响“是否发现不一致”的判定。
        assertThat(service.reconcileOnce()).isEqualTo(1);
    }

    /** Mockito 侧的多片段包含断言（AssertJ 的 contains(varargs) 不适用于参数匹配）。 */
    private static String detailContaining(String... fragments) {
        return argThat(value -> value != null
                && java.util.Arrays.stream(fragments).allMatch(value::contains));
    }

    private void givenLease() {
        AtomicReference<String> owner = new AtomicReference<>();
        when(leaseMapper.tryAcquire(eq(DocumentVectorReconciliationService.TASK_NAME),
                anyString(), any(), any())).thenAnswer(invocation -> {
                    owner.set(invocation.getArgument(1));
                    return 1;
                });
        when(leaseMapper.selectOwner(DocumentVectorReconciliationService.TASK_NAME))
                .thenAnswer(invocation -> owner.get());
    }

    private DocumentVectorReconciliationService serviceWith(ScheduledTaskLeaseMapper leases) {
        return new DocumentVectorReconciliationService(documentMapper, chunkMapper,
                versionService, agentClient, leases, PROPERTIES);
    }

    private static ReconcilableDocument document(Long id, Integer indexVersion,
                                                 String vectorCollection) {
        return new ReconcilableDocument(id, 1L, 10L, indexVersion, vectorCollection);
    }

    /** 用内存状态模拟数据库租约，验证两个实例共享同一把锁。 */
    private static final class InMemoryLeaseMapper implements ScheduledTaskLeaseMapper {

        private String owner;
        private LocalDateTime expiresAt;

        @Override
        public int tryAcquire(String taskName, String owner, LocalDateTime now,
                              LocalDateTime expiresAt) {
            if (this.owner != null && this.expiresAt != null && !this.expiresAt.isBefore(now)) {
                return 0;
            }
            this.owner = owner;
            this.expiresAt = expiresAt;
            return 1;
        }

        @Override
        public String selectOwner(String taskName) {
            return owner;
        }

        @Override
        public int release(String taskName, String owner) {
            if (this.owner != null && this.owner.equals(owner)) {
                this.owner = null;
                this.expiresAt = null;
                return 1;
            }
            return 0;
        }
    }
}
