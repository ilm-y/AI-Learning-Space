package com.ails.backend.service;

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

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.never;
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

    @BeforeAll
    static void initMyBatisLambdaCache() {
        // LambdaUpdateWrapper 依赖 MyBatis-Plus 的 TableInfo 缓存；纯 Mockito 测试
        // 不启动 MyBatis，需要手动注册一次实体元数据。
        TableInfoHelper.initTableInfo(
                new MapperBuilderAssistant(new MybatisConfiguration(), ""),
                Document.class);
    }

    @BeforeEach
    void setUp() {
        service = new DocumentIndexService(
                documentMapper, chunkService, storageService, agentClient, executor);
    }

    @Test
    void submitMarksProcessingAndEnqueuesOnce() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        doNothing().when(executor).execute(any());

        service.submit(42L);

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper).update(isNull(), captor.capture());
        assertThat(values(captor.getValue())).contains(DocumentStatus.PROCESSING);
        assertThat(values(captor.getValue())).doesNotContain(DocumentStatus.UPLOADING);
        verify(executor).execute(any());
    }

    @Test
    void submitIsNoOpWhenStatusNoLongerEligible() {
        when(documentMapper.update(isNull(), any())).thenReturn(0);

        service.submit(42L);

        verifyNoInteractions(executor);
        verify(documentMapper).update(isNull(), any());
    }

    @Test
    void rejectedTaskIsPersistedAsFailedInsteadOfStuckProcessing() {
        when(documentMapper.update(isNull(), any())).thenReturn(1);
        doThrow(new TaskRejectedException("queue full"))
                .when(executor).execute(any());

        service.submit(42L);

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper, org.mockito.Mockito.times(2)).update(isNull(), captor.capture());
        List<Wrapper<Document>> wrappers = captor.getAllValues();
        assertThat(values(wrappers.get(0))).contains(DocumentStatus.PROCESSING);
        assertThat(values(wrappers.get(1)))
                .contains(DocumentStatus.FAILED)
                .contains("索引任务队列已满，请稍后重试");
    }

    @Test
    void runIndexDoesNothingWhenDocumentIsNotProcessing() {
        Document document = document(DocumentStatus.UPLOADING);
        when(documentMapper.selectById(42L)).thenReturn(document);

        service.runIndex(42L);

        verifyNoInteractions(storageService, agentClient, chunkService);
        verify(documentMapper, never()).update(isNull(), any());
    }

    @Test
    void successfulRunWritesChunksThenVectorsThenReady() {
        Document document = document(DocumentStatus.PROCESSING);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read("space-1/doc.md")).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(new AgentParseResponse(
                "parser-v1",
                List.of(new AgentParsedChunk(
                        0, "hello", 2, true, Map.of("headingPath", List.of("T"))))));
        DocumentChunk saved = new DocumentChunk();
        saved.setId(100L);
        saved.setDocumentId(42L);
        saved.setChunkIndex(0);
        saved.setContent("hello");
        saved.setMetadataJson("{\"tokenCountEstimated\":true}");
        when(chunkService.replaceChunks(any(), any())).thenReturn(List.of(saved));
        when(agentClient.index(any(), any())).thenReturn(new AgentIndexResponse(
                "collection", "hash-test", "hash",
                List.of(new AgentVectorRef(100L, "point-100"))));
        when(documentMapper.update(isNull(), any())).thenReturn(1);

        service.runIndex(42L);

        InOrder order = inOrder(storageService, agentClient, chunkService, documentMapper);
        order.verify(storageService).read("space-1/doc.md");
        order.verify(agentClient).parse(any(), any());
        order.verify(chunkService).replaceChunks(any(), any());
        order.verify(agentClient).index(any(), any());
        order.verify(chunkService).updateVectorRefs(42L, Map.of(100L, "point-100"));
        order.verify(documentMapper).update(isNull(), any());

        ArgumentCaptor<AgentIndexRequest> request = ArgumentCaptor.forClass(AgentIndexRequest.class);
        verify(agentClient).index(any(), request.capture());
        AgentIndexChunk payload = request.getValue().chunks().get(0);
        assertThat(payload.chunkId()).isEqualTo(100L);
        assertThat(payload.content()).isEqualTo("hello");
    }

    @Test
    void finalReadyUpdateFailureFallsBackToFailed() {
        Document document = document(DocumentStatus.PROCESSING);
        when(documentMapper.selectById(42L)).thenReturn(document);
        when(storageService.read(any())).thenReturn("hello".getBytes());
        when(agentClient.parse(any(), any())).thenReturn(new AgentParseResponse(
                "parser-v1", List.of(new AgentParsedChunk(0, "hello", 2, true, Map.of()))));
        DocumentChunk saved = new DocumentChunk();
        saved.setId(100L);
        saved.setChunkIndex(0);
        saved.setContent("hello");
        when(chunkService.replaceChunks(any(), any())).thenReturn(List.of(saved));
        when(agentClient.index(any(), any())).thenReturn(new AgentIndexResponse(
                "collection", "hash-test", "hash",
                List.of(new AgentVectorRef(100L, "point-100"))));
        // 第一次是 complete（条件更新失败），第二次是 fail 写入。
        when(documentMapper.update(isNull(), any())).thenReturn(0, 1);

        service.runIndex(42L);

        ArgumentCaptor<Wrapper<Document>> captor = updateCaptor();
        verify(documentMapper, org.mockito.Mockito.times(2)).update(isNull(), captor.capture());
        assertThat(values(captor.getAllValues().get(0))).contains(DocumentStatus.READY);
        assertThat(values(captor.getAllValues().get(1)))
                .contains(DocumentStatus.FAILED)
                .contains("文档状态已变化，索引结果未标记为 READY");
    }

    // ===================== helpers =====================

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static ArgumentCaptor<Wrapper<Document>> updateCaptor() {
        return (ArgumentCaptor) ArgumentCaptor.forClass(Wrapper.class);
    }

    private static List<Object> values(Wrapper<Document> wrapper) {
        assertThat(wrapper).isInstanceOf(LambdaUpdateWrapper.class);
        LambdaUpdateWrapper<Document> lambda = (LambdaUpdateWrapper<Document>) wrapper;
        // getSqlSet() 会生成 set 片段；参数值在生成过程中写入 paramNameValuePairs。
        assertThat(lambda.getSqlSet()).contains("status");
        // 某些 set 值可能就是 null（例如清空 index_error），List.copyOf 会拒绝 null。
        return new java.util.ArrayList<>(lambda.getParamNameValuePairs().values());
    }

    private static Document document(DocumentStatus status) {
        Document document = new Document();
        document.setId(42L);
        document.setLearningSpaceId(1L);
        document.setUserId(7L);
        document.setName("doc");
        document.setFileName("doc.md");
        document.setFileType("md");
        document.setStorageUri("space-1/doc.md");
        document.setStatus(status);
        document.setIndexAttempts(0);
        document.setIndexVersion(0);
        return document;
    }
}
