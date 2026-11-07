package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.dto.document.agent.AgentIndexChunk;
import com.ails.backend.dto.document.agent.AgentIndexRequest;
import com.ails.backend.dto.document.agent.AgentIndexResponse;
import com.ails.backend.dto.document.agent.AgentParseResponse;
import com.ails.backend.dto.document.agent.AgentParsedChunk;
import com.ails.backend.dto.document.agent.AgentVectorRef;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
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
import org.mockito.InOrder;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.task.TaskExecutor;
import org.springframework.core.task.TaskRejectedException;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class DocumentIndexServiceTest {

    @Mock
    private DocumentMapper documentMapper;

    @Mock
    private DocumentChunkService chunkService;

    @Mock
    private DocumentStorageService storageService;

    @Mock
    private AgentDocumentClient agentClient;

    @Mock
    private TaskExecutor executor;

    private DocumentIndexService service;

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
        service = new DocumentIndexService(
                documentMapper, chunkService, storageService, agentClient, PROPERTIES, executor);
    }

    // ===================== submit / lease =====================

    @Test
    void submitAcquiresLeaseAndEnqueuesAttemptToken() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        when(documentMapper.selectById(42L)).thenReturn(document(DocumentStatus.PROCESSING, 1));

        service.submit(42L);

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        assertThat(sqlSet(captor.getValue()))
                .contains("status = CASE WHEN status = 'READY' THEN 'READY' ELSE 'PROCESSING' END")
                .contains("index_attempts = index_attempts + 1");
        verify(executor).execute(any());
    }

    @Test
    void submitIsNoOpWhenLeaseCannotBeAcquired() {
        when(documentMapper.update(isNull(), any())).thenReturn(0);

        service.submit(42L);

        verifyNoInteractions(executor);
        verify(documentMapper).update(isNull(), any());
    }

    @Test
    void rejectedTaskReleasesLeaseWithFailure() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        when(documentMapper.selectById(42L)).thenReturn(document(DocumentStatus.PROCESSING, 1));
        doThrow(new TaskRejectedException("queue full")).when(executor).execute(any());

        service.submit(42L);

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper, times(2)).update(isNull(), captor.capture());
        // 第二次是 fail：READY 保持不变，PROCESSING 才转 FAILED。
        assertThat(sqlSet(captor.getAllValues().get(1)))
                .contains("status = CASE WHEN status = 'PROCESSING' THEN 'FAILED' ELSE status END");
        assertThat(values(captor.getAllValues().get(1)))
                .contains("索引任务队列已满，请稍后重试");
    }

    @Test
    void runIndexAbandonsWhenLeaseOwnerChanged() {
        Document document = document(DocumentStatus.PROCESSING, 2);
        when(documentMapper.selectById(42L)).thenReturn(document);

        service.runIndex(42L, 1);

        verifyNoInteractions(storageService, agentClient, chunkService);
        verify(documentMapper, never()).update(isNull(), any());
    }

    // ===================== success path =====================

    @Test
    void successfulRunStagesVersionBeforeSwitchingActiveIndex() {
        Document document = document(DocumentStatus.PROCESSING, 1);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read("space-1/doc.md")).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(parseResponse());
        DocumentChunk saved = savedChunk();
        when(chunkService.stageChunks(any(), any(), eq(1))).thenReturn(List.of(saved));
        when(agentClient.index(any(), any())).thenReturn(indexResponse());
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        when(chunkService.listOtherVersions(42L, 1)).thenReturn(List.of());

        service.runIndex(42L, 1);

        InOrder order = inOrder(storageService, agentClient, chunkService, documentMapper);
        order.verify(storageService).read("space-1/doc.md");
        order.verify(agentClient).parse(any(), any());
        order.verify(chunkService).stageChunks(any(), any(), eq(1));
        order.verify(agentClient).index(any(), any());
        order.verify(chunkService).updateVectorRefs(42L, 1, Map.of(100L, "point-100"));
        order.verify(documentMapper).update(isNull(), any());
        // 本次构建没有更早版本可清理。
        verify(chunkService, never()).deleteVersions(any(), any());

        ArgumentCaptor<AgentIndexRequest> request = ArgumentCaptor.forClass(AgentIndexRequest.class);
        verify(agentClient).index(any(), request.capture());
        assertThat(request.getValue().indexVersion()).isEqualTo(1);
        AgentIndexChunk payload = request.getValue().chunks().get(0);
        assertThat(payload.chunkId()).isEqualTo(100L);
        assertThat(payload.content()).isEqualTo("hello");
    }

    @Test
    void cleanupFailureAfterSwitchDoesNotFlipReadyBack() {
        Document document = document(DocumentStatus.PROCESSING, 1);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read(any())).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(parseResponse());
        when(chunkService.stageChunks(any(), any(), eq(1))).thenReturn(List.of(savedChunk()));
        when(agentClient.index(any(), any())).thenReturn(indexResponse());
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        when(chunkService.listOtherVersions(42L, 1)).thenReturn(List.of(0));
        doThrow(new RuntimeException("qdrant cleanup unavailable"))
                .when(agentClient).deleteVersions(any(), any(), eq(true), any());

        service.runIndex(42L, 1);

        // 只有 complete 一次更新；清理失败不能再写 fail。
        verify(documentMapper, times(1)).update(isNull(), any());
    }

    // ===================== failure path =====================

    @Test
    void indexFailureRollsBackOnlyNewVersionAndKeepsOldIndex() {
        Document document = document(DocumentStatus.READY, 2);
        document.setIndexVersion(1);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read(any())).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(parseResponse());
        when(chunkService.stageChunks(any(), any(), eq(2))).thenReturn(List.of(savedChunk()));
        when(agentClient.index(any(), any()))
                .thenThrow(new DocumentIndexException(503, "embedding unavailable"));
        when(documentMapper.update(isNull(), any())).thenReturn(1);

        service.runIndex(42L, 2);

        // 只删本次 attempt 的 chunk；活跃版本 1 的 chunk 不动。
        verify(chunkService).deleteVersion(42L, 2);
        verify(chunkService, never()).deleteVersions(any(), any());
        // READY 重建失败保持 READY。
        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        assertThat(sqlSet(captor.getValue()))
                .contains("CASE WHEN status = 'PROCESSING' THEN 'FAILED' ELSE status END");
        assertThat(values(captor.getValue())).contains("embedding unavailable");
    }

    @Test
    void vectorRollbackFailureKeepsStagedChunkForCleanupRetry() {
        Document document = document(DocumentStatus.READY, 2);
        document.setIndexVersion(1);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read(any())).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(parseResponse());
        when(chunkService.stageChunks(any(), any(), eq(2))).thenReturn(List.of(savedChunk()));
        when(agentClient.index(any(), any()))
                .thenThrow(new DocumentIndexException(503, "embedding unavailable"));
        doThrow(new DocumentIndexException(503, "qdrant unavailable"))
                .when(agentClient).deleteVersions(any(), any(), eq(false), any());
        when(documentMapper.update(isNull(), any())).thenReturn(1);

        service.runIndex(42L, 2);

        // 向量没清掉时必须保留 chunk 记录，供定时清理任务重新发现孤儿 points。
        verify(chunkService, never()).deleteVersion(any(), anyInt());
        // 文档仍保持 READY（CASE），旧版本继续服务。
        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        assertThat(sqlSet(captor.getValue()))
                .contains("CASE WHEN status = 'PROCESSING' THEN 'FAILED' ELSE status END");
    }

    @Test
    void partialVectorResponseFailsWithoutSwitchingActiveVersion() {
        Document document = document(DocumentStatus.PROCESSING, 1);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read(any())).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(parseResponse());
        when(chunkService.stageChunks(any(), any(), eq(1))).thenReturn(List.of(savedChunk()));
        // 声明 1 个 chunk，只返回 0 个 vector：属于不完整写入。
        when(agentClient.index(any(), any())).thenReturn(new AgentIndexResponse(
                "collection", "hash-test", "hash", 256, "v1:fingerprint", List.of()));
        when(documentMapper.update(isNull(), any())).thenReturn(1);

        service.runIndex(42L, 1);

        verify(chunkService, never()).updateVectorRefs(any(), anyInt(), any());
        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        assertThat(values(captor.getValue()))
                .anySatisfy(value -> assertThat(String.valueOf(value)).contains("向量引用数量"));
    }

    @Test
    void completeFailsWhenAttemptWasFenced() {
        DocumentIndexService.ValidatedIndex indexed = new DocumentIndexService.ValidatedIndex(
                Map.of(100L, "point-100"), "collection", "hash-test", "hash", 256, "v1:f");
        when(documentMapper.update(isNull(), any())).thenReturn(0);

        assertThatThrownBy(() -> service.complete(42L, 1, 1, "parser-v1", indexed))
                .isInstanceOf(DocumentIndexException.class)
                .hasMessageContaining("未标记为 READY");
    }

    @Test
    void failTruncatesLongError() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);

        service.fail(42L, 1, "x".repeat(5000));

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        String error = (String) values(captor.getValue()).stream()
                .filter(value -> value instanceof String text && text.length() > 100)
                .findFirst().orElseThrow();
        assertThat(error).hasSize(1024);
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

    private static AgentParseResponse parseResponse() {
        return new AgentParseResponse(
                "parser-v1",
                List.of(new AgentParsedChunk(
                        0, "hello", 2, true, Map.of("headingPath", List.of("T")))));
    }

    private static AgentIndexResponse indexResponse() {
        return new AgentIndexResponse(
                "collection", "hash-test", "hash", 256, "v1:fingerprint",
                List.of(new AgentVectorRef(100L, "point-100")));
    }

    private static DocumentChunk savedChunk() {
        DocumentChunk saved = new DocumentChunk();
        saved.setId(100L);
        saved.setDocumentId(42L);
        saved.setIndexVersion(1);
        saved.setChunkIndex(0);
        saved.setContent("hello");
        saved.setMetadataJson("{\"tokenCountEstimated\":true}");
        return saved;
    }

    private static Document document(DocumentStatus status, int attempts) {
        Document document = new Document();
        document.setId(42L);
        document.setLearningSpaceId(1L);
        document.setUserId(7L);
        document.setName("doc");
        document.setFileName("doc.md");
        document.setFileType("md");
        document.setStorageUri("space-1/doc.md");
        document.setStatus(status);
        document.setIndexAttempts(attempts);
        document.setIndexVersion(0);
        document.setIndexStartedAt(LocalDateTime.now());
        return document;
    }
}
