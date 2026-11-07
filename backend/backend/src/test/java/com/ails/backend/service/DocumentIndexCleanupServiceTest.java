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

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 旧索引版本延迟清理：只清理非活跃版本，构建中的版本绝不当作旧版本删除。
 */
@ExtendWith(MockitoExtension.class)
class DocumentIndexCleanupServiceTest {

    @Mock
    private DocumentMapper documentMapper;

    @Mock
    private DocumentChunkService chunkService;

    @Mock
    private AgentDocumentClient agentClient;

    private DocumentIndexCleanupService service;

    private static final DocumentIndexProperties PROPERTIES = new DocumentIndexProperties(
            2, 2, 16, 5_000, 5_000, 60_000, 3_600_000, 3_600_000, 100,
            120_000, 3, 30_000, 60_000, 30_000, 50, 20,
            300_000, 120_000, 50);

    @BeforeEach
    void setUp() {
        service = new DocumentIndexCleanupService(
                documentMapper, chunkService, agentClient, PROPERTIES);
    }

    @Test
    void deletesOnlyNonActiveVersions() {
        when(documentMapper.selectById(9L)).thenReturn(document(3, null));
        when(chunkService.listOtherVersions(9L, 3)).thenReturn(List.of(1, 2));

        service.cleanup(9L);

        verify(agentClient).deleteOtherVersions(
                any(Document.class), org.mockito.ArgumentMatchers.eq(3), any());
        verify(chunkService).deleteOtherVersions(9L, 3);
    }

    @Test
    void skipsDocumentWithActiveBuildLease() {
        // 有 index_started_at 说明正在构建：staged chunk 不能当成旧版本删除。
        when(documentMapper.selectById(9L))
                .thenReturn(document(3, LocalDateTime.now()));

        service.cleanup(9L);

        verifyNoInteractions(chunkService, agentClient);
    }

    @Test
    void skipsDocumentWithoutActiveVersion() {
        when(documentMapper.selectById(9L)).thenReturn(document(null, null));

        service.cleanup(9L);

        verifyNoInteractions(chunkService, agentClient);
    }

    @Test
    void skipsWhenThereAreNoStaleVersions() {
        when(documentMapper.selectById(9L)).thenReturn(document(3, null));
        when(chunkService.listOtherVersions(9L, 3)).thenReturn(List.of());

        service.cleanup(9L);

        verifyNoInteractions(agentClient);
        verify(chunkService, never()).deleteOtherVersions(anyLong(), anyInt());
    }

    @Test
    void agentFailureLeavesChunksForRetry() {
        when(documentMapper.selectById(9L)).thenReturn(document(3, null));
        when(chunkService.listOtherVersions(9L, 3)).thenReturn(List.of(1));
        doThrow(new DocumentIndexException(503, "agent down"))
                .when(agentClient).deleteOtherVersions(any(), anyInt(), any());

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> service.cleanup(9L))
                .isInstanceOf(DocumentIndexException.class);

        // Agent 失败时不能先删 MySQL chunk，否则下一轮扫描无法再定位该文档。
        verify(chunkService, never()).deleteOtherVersions(anyLong(), anyInt());
    }

    @Test
    void scanFailureDoesNotBlockProcess() {
        when(documentMapper.selectDocumentsWithStaleVersions(PROPERTIES.cleanupBatchSize()))
                .thenThrow(new RuntimeException("db down"));

        service.cleanupStaleVersions();

        verifyNoInteractions(chunkService, agentClient);
    }

    private static Document document(Integer indexVersion, LocalDateTime startedAt) {
        Document document = new Document();
        document.setId(9L);
        document.setStatus(DocumentStatus.READY);
        document.setIndexVersion(indexVersion);
        document.setIndexStartedAt(startedAt);
        document.setVectorCollection("collection");
        return document;
    }
}
