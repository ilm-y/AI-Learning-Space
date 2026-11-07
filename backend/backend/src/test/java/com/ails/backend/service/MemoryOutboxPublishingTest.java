package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 设计文档 §4.1.1：各写入口何时产生 Outbox 事件。
 *
 * <p>重点覆盖「不索引候选」与「ACTIVE 无实际变化不发事件」，
 * 这两条是避免向量库被无意义写入淹没的关键。</p>
 */
@ExtendWith(MockitoExtension.class)
class MemoryOutboxPublishingTest {

    private static final long USER_ID = 1L;
    private static final long MEMORY_ID = 42L;

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

    private MemoryService service;

    @BeforeEach
    void setUp() {
        service = new MemoryService(memoryMapper, memorySourceMapper, spaceService,
                conceptService, outboxPublisher, hybridSearchService);
        lenient().when(memoryMapper.selectCount(any())).thenReturn(0L);
        lenient().when(memoryMapper.updateById(any(Memory.class))).thenReturn(1);
        lenient().when(memoryMapper.insert(any(Memory.class))).thenAnswer(invocation -> {
            invocation.<Memory>getArgument(0).setId(MEMORY_ID);
            return 1;
        });
    }

    // ==================== create ====================

    @Test
    @DisplayName("create（ACTIVE）：发 MEMORY_UPSERT")
    void createPublishesUpsert() {
        service.create(USER_ID, request("k", "new", 0.6f, MemoryStatus.ACTIVE));

        ArgumentCaptor<Memory> captor = ArgumentCaptor.forClass(Memory.class);
        verify(outboxPublisher).publishUpsert(captor.capture());
        assertThat(captor.getValue().getId()).isEqualTo(MEMORY_ID);
        assertThat(captor.getValue().getStatus()).isEqualTo(MemoryStatus.ACTIVE);
        assertThat(captor.getValue().getUserId()).isEqualTo(USER_ID);
    }

    @Test
    @DisplayName("createCandidate（CANDIDATE）：不发事件（候选不索引）")
    void createCandidatePublishesNothing() {
        service.createCandidate(USER_ID, request("k", "candidate", 0.6f, MemoryStatus.ACTIVE),
                MemorySourceType.MESSAGE, 100L);

        verifyNoInteractions(outboxPublisher);
    }

    // ==================== update ====================

    @Test
    @DisplayName("update CANDIDATE → ACTIVE：补发 MEMORY_UPSERT")
    void candidateConfirmedPublishesUpsert() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.CANDIDATE, "old", 0.5f));

        service.update(USER_ID, MEMORY_ID,
                request("k", "confirmed", 0.7f, MemoryStatus.ACTIVE));

        ArgumentCaptor<Memory> captor = ArgumentCaptor.forClass(Memory.class);
        verify(outboxPublisher).publishUpsert(captor.capture());
        assertThat(captor.getValue().getStatus()).isEqualTo(MemoryStatus.ACTIVE);
        assertThat(captor.getValue().getContent()).isEqualTo("confirmed");
    }

    @Test
    @DisplayName("update ACTIVE → ACTIVE 且索引字段变化：发 MEMORY_UPSERT")
    void activeContentChangePublishesUpsert() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.ACTIVE, "old", 0.5f));

        service.update(USER_ID, MEMORY_ID,
                request("k", "new content", 0.5f, MemoryStatus.ACTIVE));

        verify(outboxPublisher).publishUpsert(any(Memory.class));
    }

    @Test
    @DisplayName("update ACTIVE → ACTIVE 无实际变化：不发事件，但仍正常写库")
    void activeNoOpUpdatePublishesNothing() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.ACTIVE, "same", 0.5f));

        service.update(USER_ID, MEMORY_ID, request("k", "same", 0.5f, null));

        verify(memoryMapper).updateById(any(Memory.class));
        verifyNoInteractions(outboxPublisher);
    }

    @Test
    @DisplayName("update ACTIVE → ARCHIVED：发 MEMORY_DELETE")
    void activeArchivedByUpdatePublishesDelete() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.ACTIVE, "old", 0.5f));

        service.update(USER_ID, MEMORY_ID,
                request("k", "old", 0.5f, MemoryStatus.ARCHIVED));

        verify(outboxPublisher).publishDelete(any(Memory.class));
        verify(outboxPublisher, never()).publishUpsert(any(Memory.class));
    }

    @Test
    @DisplayName("update CANDIDATE → ARCHIVED：不发事件（从未索引）")
    void candidateArchivedByUpdatePublishesNothing() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.CANDIDATE, "old", 0.5f));

        service.update(USER_ID, MEMORY_ID,
                request("k", "old", 0.5f, MemoryStatus.ARCHIVED));

        verifyNoInteractions(outboxPublisher);
    }

    @Test
    @DisplayName("update 乐观锁失败（409）：不发事件")
    void optimisticLockFailurePublishesNothing() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.ACTIVE, "old", 0.5f));
        when(memoryMapper.updateById(any(Memory.class))).thenReturn(0);

        assertThatThrownBy(() -> service.update(USER_ID, MEMORY_ID,
                request("k", "new", 0.5f, MemoryStatus.ACTIVE)))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(409));

        verifyNoInteractions(outboxPublisher);
    }

    // ==================== archive ====================

    @Test
    @DisplayName("archive ACTIVE：发 MEMORY_DELETE")
    void archiveActivePublishesDelete() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.ACTIVE, "old", 0.5f));

        service.archive(USER_ID, MEMORY_ID);

        ArgumentCaptor<Memory> captor = ArgumentCaptor.forClass(Memory.class);
        verify(outboxPublisher).publishDelete(captor.capture());
        assertThat(captor.getValue().getStatus()).isEqualTo(MemoryStatus.ARCHIVED);
    }

    @Test
    @DisplayName("archive CANDIDATE：不发事件")
    void archiveCandidatePublishesNothing() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.CANDIDATE, "old", 0.5f));

        service.archive(USER_ID, MEMORY_ID);

        verifyNoInteractions(outboxPublisher);
    }

    @Test
    @DisplayName("archive ARCHIVED：幂等短路，不写库也不发事件")
    void archiveArchivedIsIdempotentWithoutEvent() {
        when(memoryMapper.selectById(MEMORY_ID))
                .thenReturn(memory(MemoryStatus.ARCHIVED, "old", 0.5f));

        service.archive(USER_ID, MEMORY_ID);

        verify(memoryMapper, never()).updateById(any(Memory.class));
        verifyNoInteractions(outboxPublisher);
    }

    // ==================== helpers ====================

    private static Memory memory(MemoryStatus status, String content, float importance) {
        Memory memory = new Memory();
        memory.setId(MEMORY_ID);
        memory.setUserId(USER_ID);
        memory.setScopeType(MemoryScopeType.GLOBAL);
        memory.setScopeKey("GLOBAL");
        memory.setMemoryKey("k");
        memory.setMemoryType(MemoryType.LEARNING);
        memory.setContent(content);
        memory.setImportance(importance);
        memory.setStatus(status);
        memory.setVersion(0);
        return memory;
    }

    private static MemoryRequest request(String memoryKey, String content,
                                         float importance, MemoryStatus status) {
        return new MemoryRequest(null, null, MemoryType.LEARNING, memoryKey, content,
                importance, status);
    }
}
