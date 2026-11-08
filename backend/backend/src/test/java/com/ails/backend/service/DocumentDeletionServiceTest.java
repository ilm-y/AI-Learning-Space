package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.entity.Document;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.conditions.Wrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.task.TaskExecutor;

import java.time.LocalDateTime;
import java.util.List;
import java.util.concurrent.RejectedExecutionException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class DocumentDeletionServiceTest {

    @Mock
    private DocumentMapper documentMapper;

    @Mock
    private DocumentChunkService chunkService;

    @Mock
    private DocumentIndexVersionService versionService;

    @Mock
    private DocumentStorageService storageService;

    @Mock
    private AgentDocumentClient agentClient;

    @Mock
    private TaskExecutor executor;

    private DocumentDeletionService service;

    private static final DocumentIndexProperties PROPERTIES = new DocumentIndexProperties(
            2, 2, 16, 5_000, 5_000, 60_000, 3_600_000, 3_600_000, 100,
            120_000, 3, 30_000, 60_000, 30_000, 50, 20,
            300_000, 120_000, 50);

    @BeforeAll
    static void initMyBatisLambdaCache() {
        TableInfoHelper.initTableInfo(
                new MapperBuilderAssistant(new MybatisConfiguration(), ""),
                Document.class);
    }

    @BeforeEach
    void setUp() {
        service = new DocumentDeletionService(documentMapper, chunkService, versionService,
                storageService, agentClient, PROPERTIES, executor);
    }

    // ===================== request delete =====================

    @Test
    void requestDeleteSwitchesToDeletingAndFencesRunningIndex() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);

        service.requestDelete(document(DocumentStatus.READY, 1, null));

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        assertThat(sqlSet(captor.getValue()))
                .contains("index_attempts = index_attempts + 1");
        assertThat(values(captor.getValue())).contains(DocumentStatus.DELETING);
        verify(executor).execute(any());
    }

    @Test
    void repeatedDeleteWhileDeletingOnlyEnqueuesAgain() {
        Document deleting = document(DocumentStatus.DELETING, 1, null);

        service.requestDelete(deleting);

        verify(documentMapper, never()).update(isNull(), any());
        verify(executor).execute(any());
    }

    @Test
    void deleteOfAlreadyDeletedDocumentIsNoOp() {
        service.requestDelete(document(DocumentStatus.DELETED, 1, LocalDateTime.now()));

        verifyNoInteractions(documentMapper, executor);
    }

    @Test
    void concurrentDeletingStateAfterLosingRaceStillEnqueues() {
        when(documentMapper.update(isNull(), any())).thenReturn(0);
        when(documentMapper.selectAnyById(9L))
                .thenReturn(document(DocumentStatus.DELETING, 1, null));

        service.requestDelete(document(DocumentStatus.READY, 1, null));

        verify(executor).execute(any());
    }

    @Test
    void rejectedCleanupTaskKeepsDeletingForScheduledRetry() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        doThrow(new RejectedExecutionException("queue full")).when(executor).execute(any());

        service.requestDelete(document(DocumentStatus.READY, 1, null));

        // 状态已经切成 DELETING；队列拒绝时不能抛给调用方，交给定时任务重试。
        verify(documentMapper).update(isNull(), any());
    }

    // ===================== cleanup =====================

    @Test
    void cleanupDeletesVectorsChunksFileThenMarksDeleted() {
        when(documentMapper.selectAnyById(9L))
                .thenReturn(document(DocumentStatus.DELETING, 1, null));
        when(documentMapper.markDeleted(eq(9L), any(LocalDateTime.class))).thenReturn(1);

        service.cleanup(9L);

        verify(agentClient).deleteDocument(any());
        verify(chunkService).deleteAll(9L);
        verify(storageService).deleteQuietly("space-1/doc.md");
        verify(documentMapper).markDeleted(eq(9L), any(LocalDateTime.class));
        verify(documentMapper, never()).recordDeleteFailure(anyLong(), any());
    }

    @Test
    void vectorCleanupFailureKeepsDeletingAndRecordsError() {
        when(documentMapper.selectAnyById(9L))
                .thenReturn(document(DocumentStatus.DELETING, 1, null));
        doThrow(new DocumentIndexException(503, "qdrant unavailable"))
                .when(agentClient).deleteDocument(any());

        service.cleanup(9L);

        // 向量没删掉绝不能置 DELETED；保持 DELETING 等待定时重试。
        verify(documentMapper, never()).markDeleted(anyLong(), any());
        verify(chunkService, never()).deleteAll(anyLong());
        verify(storageService, never()).deleteQuietly(any());
        verify(documentMapper).recordDeleteFailure(eq(9L), any());
    }

    @Test
    void repeatedCleanupIsIdempotentAfterDocumentDeleted() {
        when(documentMapper.selectAnyById(9L))
                .thenReturn(document(DocumentStatus.DELETED, 1, LocalDateTime.now()));

        service.cleanup(9L);

        verifyNoInteractions(agentClient, chunkService, storageService);
        verify(documentMapper, never()).markDeleted(anyLong(), any());
    }

    @Test
    void cleanupSkipsDocumentNoLongerDeleting() {
        when(documentMapper.selectAnyById(9L))
                .thenReturn(document(DocumentStatus.READY, 1, null));

        service.cleanup(9L);

        verifyNoInteractions(agentClient, chunkService, storageService);
    }

    @Test
    void scheduledRetryEnqueuesEveryPendingDocument() {
        when(documentMapper.selectDeletingIds(PROPERTIES.deleteMaxAttempts(), PROPERTIES.deleteBatchSize()))
                .thenReturn(List.of(1L, 2L, 3L));

        service.retryPendingDeletes();

        verify(executor, times(3)).execute(any());
    }

    @Test
    void scanFailureDoesNotAttemptAnyCleanup() {
        when(documentMapper.selectDeletingIds(anyInt(), anyInt()))
                .thenThrow(new RuntimeException("db down"));

        service.retryPendingDeletes();

        verifyNoInteractions(executor);
    }

    @Test
    void cleanupTaskRunsSynchronouslyWhenExecutorAcceptsIt() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        doAnswer(invocation -> {
            ((Runnable) invocation.getArgument(0)).run();
            return null;
        }).when(executor).execute(any());
        when(documentMapper.selectAnyById(9L))
                .thenReturn(document(DocumentStatus.DELETING, 1, null));
        when(documentMapper.markDeleted(eq(9L), any(LocalDateTime.class))).thenReturn(1);

        service.requestDelete(document(DocumentStatus.READY, 1, null));

        verify(agentClient).deleteDocument(any());
        verify(documentMapper).markDeleted(eq(9L), any(LocalDateTime.class));
    }

    // ===================== helpers =====================

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static ArgumentCaptor<Wrapper<Document>> updateCaptor() {
        return (ArgumentCaptor) ArgumentCaptor.forClass(Wrapper.class);
    }

    private static List<Object> values(Wrapper<Document> wrapper) {
        assertThat(wrapper).isInstanceOf(LambdaUpdateWrapper.class);
        LambdaUpdateWrapper<Document> lambda = (LambdaUpdateWrapper<Document>) wrapper;
        return new java.util.ArrayList<>(lambda.getParamNameValuePairs().values());
    }

    private static String sqlSet(Wrapper<Document> wrapper) {
        assertThat(wrapper).isInstanceOf(LambdaUpdateWrapper.class);
        String sql = ((LambdaUpdateWrapper<Document>) wrapper).getSqlSet();
        return sql == null ? "" : sql;
    }

    private static Document document(DocumentStatus status, int attempts, LocalDateTime deletedAt) {
        Document document = new Document();
        document.setId(9L);
        document.setLearningSpaceId(1L);
        document.setUserId(7L);
        document.setName("doc");
        document.setFileName("doc.md");
        document.setFileType("md");
        document.setStorageUri("space-1/doc.md");
        document.setStatus(status);
        document.setIndexAttempts(attempts);
        document.setIndexVersion(0);
        document.setDeletedAt(deletedAt);
        return document;
    }
}
