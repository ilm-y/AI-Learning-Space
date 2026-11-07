package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.entity.Document;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 崩溃回收：PROCESSING → FAILED；READY 重建租约 → 保持 READY；UPLOADING → 重提/耗尽。
 */
@ExtendWith(MockitoExtension.class)
class DocumentIndexRecoveryServiceTest {

    @Mock
    private DocumentMapper documentMapper;

    @Mock
    private DocumentIndexService indexService;

    private DocumentIndexRecoveryService service;

    private static final DocumentIndexProperties PROPERTIES = new DocumentIndexProperties(
            2, 2, 16, 5_000, 5_000, 60_000, 3_600_000, 3_600_000, 100,
            120_000, 3, 30_000, 60_000, 30_000, 50, 20,
            300_000, 120_000, 50);

    @BeforeEach
    void setUp() {
        service = new DocumentIndexRecoveryService(documentMapper, indexService, PROPERTIES);
    }

    @Test
    void staleProcessingIsMarkedFailedWithFencingTime() {
        when(documentMapper.selectStaleProcessingIds(any(LocalDateTime.class), eq(100)))
                .thenReturn(List.of(1L));
        when(documentMapper.selectStaleReadyRebuildIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.selectStaleUploadingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.markStaleProcessingFailed(eq(1L), any(LocalDateTime.class), any()))
                .thenReturn(1);

        int changed = service.recoverStale(LocalDateTime.now());

        assertThat(changed).isEqualTo(1);
        verify(documentMapper).markStaleProcessingFailed(
                eq(1L), any(LocalDateTime.class), any());
        verify(indexService, never()).submit(anyLong());
    }

    @Test
    void staleReadyRebuildOnlyReleasesLeaseAndKeepsReady() {
        when(documentMapper.selectStaleProcessingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.selectStaleReadyRebuildIds(any(LocalDateTime.class), eq(100)))
                .thenReturn(List.of(2L));
        when(documentMapper.selectStaleUploadingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.markStaleReadyRebuildAbandoned(
                eq(2L), any(LocalDateTime.class), any())).thenReturn(1);

        int changed = service.recoverStale(LocalDateTime.now());

        assertThat(changed).isEqualTo(1);
        // 绝不能调用 markStaleProcessingFailed 把 READY 文档打成 FAILED。
        verify(documentMapper, never()).markStaleProcessingFailed(
                anyLong(), any(), any());
    }

    @Test
    void staleUploadingIsResubmittedBeforeMaxAttempts() {
        when(documentMapper.selectStaleProcessingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.selectStaleReadyRebuildIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.selectStaleUploadingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of(3L));
        when(documentMapper.selectById(3L)).thenReturn(document(3L, 1));

        int changed = service.recoverStale(LocalDateTime.now());

        assertThat(changed).isEqualTo(1);
        verify(indexService).submit(3L);
        verify(documentMapper, never()).markUploadingFailed(anyLong(), any());
    }

    @Test
    void staleUploadingBeyondMaxAttemptsIsFailed() {
        when(documentMapper.selectStaleProcessingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.selectStaleReadyRebuildIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of());
        when(documentMapper.selectStaleUploadingIds(any(LocalDateTime.class), anyInt()))
                .thenReturn(List.of(4L));
        when(documentMapper.selectById(4L)).thenReturn(document(4L, 3));

        service.recoverStale(LocalDateTime.now());

        verify(indexService, never()).submit(4L);
        verify(documentMapper).markUploadingFailed(eq(4L), any());
    }

    private static Document document(Long id, int attempts) {
        Document document = new Document();
        document.setId(id);
        document.setStatus(DocumentStatus.UPLOADING);
        document.setIndexAttempts(attempts);
        return document;
    }
}
