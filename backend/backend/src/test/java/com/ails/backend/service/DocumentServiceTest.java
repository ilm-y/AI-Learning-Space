package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.config.DocumentStorageProperties;
import com.ails.backend.config.RetrievalProperties;
import com.ails.backend.dto.document.DocumentResponse;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.dto.document.LegacyReadyDocument;
import com.ails.backend.dto.document.LegacyReadyReport;
import com.ails.backend.dto.document.LegacyReadySummary;
import com.ails.backend.dto.document.ReadyDocument;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.LearningSpace;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.mock.web.MockMultipartFile;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 业务层测试：数据库 mapper 用 Mockito，文件存储使用真实临时目录。
 * <p>
 * 这样能同时验证「文件确实被写入/清理」和「数据库失败分支」，又不需要 MySQL。
 * 真正的并发唯一键、{@code @TableLogic} 软删过滤由
 * {@code DocumentServiceIntegrationTest} 在独立 MySQL 库中验证。
 */
@ExtendWith(MockitoExtension.class)
class DocumentServiceTest {

    @Mock
    private DocumentMapper documentMapper;

    @Mock
    private LearningSpaceService spaceService;

    @Mock
    private DocumentIndexService indexService;

    @Mock
    private DocumentDeletionService deletionService;

    @TempDir
    Path root;

    private DocumentStorageService storageService;
    private DocumentService service;
    private LearningSpace space;

    @BeforeEach
    void setUp() {
        storageService = new DocumentStorageService(
                new DocumentStorageProperties(root.toString(), 1024 * 1024));
        service = serviceWith(new RetrievalProperties(false));

        space = new LearningSpace();
        space.setId(10L);
        space.setUserId(1L);
        space.setName("space");
    }

    @Test
    void uploadsFileAndPersistsUploadingDocument() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectOne(any())).thenReturn(null);
        when(documentMapper.insert(any(Document.class))).thenAnswer(invocation -> {
            Document document = invocation.getArgument(0);
            document.setId(99L);
            return 1;
        });

        DocumentUploadResponse result = service.upload(1L, 10L, markdown("notes.md", "# TLB"));

        assertThat(result.duplicate()).isFalse();
        assertThat(result.document().id()).isEqualTo(99L);
        assertThat(result.document().learningSpaceId()).isEqualTo(10L);
        assertThat(result.document().userId()).isEqualTo(1L);
        assertThat(result.document().name()).isEqualTo("notes");
        assertThat(result.document().fileName()).isEqualTo("notes.md");
        assertThat(result.document().fileType()).isEqualTo("md");
        assertThat(result.document().size()).isEqualTo(5L);
        assertThat(result.document().status()).isEqualTo(DocumentStatus.UPLOADING);
        assertThat(result.document().status()).isNotEqualTo(DocumentStatus.READY);
        assertThat(result.document().chunkCount()).isZero();
        assertThat(result.document().indexAttempts()).isZero();
        assertThat(result.document().indexVersion()).isZero();

        verify(indexService).submit(99L);

        ArgumentCaptor<Document> captor = ArgumentCaptor.forClass(Document.class);
        verify(documentMapper).insert(captor.capture());
        Document inserted = captor.getValue();
        assertThat(inserted.getUserId()).isEqualTo(1L);
        assertThat(inserted.getStatus()).isEqualTo(DocumentStatus.UPLOADING);
        assertThat(inserted.getStorageUri()).matches("space-10/[0-9a-f]{32}\\.md");
        assertThat(inserted.getContentHash()).matches("[0-9a-f]{64}");
        assertThat(root.resolve(inserted.getStorageUri())).exists();
        assertThat(regularFiles()).hasSize(1);
    }

    @Test
    void duplicateContentReturnsExistingWithoutWritingFile() {
        Document existing = existingDocument(55L);
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectOne(any())).thenReturn(existing);

        DocumentUploadResponse result = service.upload(1L, 10L, markdown("notes.md", "# TLB"));

        assertThat(result.duplicate()).isTrue();
        assertThat(result.document().id()).isEqualTo(55L);
        verify(documentMapper, never()).insert(any(Document.class));
        assertThat(regularFiles()).isEmpty();
    }

    @Test
    void duplicateKeyRaceDeletesLosingFileAndReturnsWinner() {
        Document existing = existingDocument(55L);
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        // 第一次重复检查为空；insert 撞唯一键后第二次查询读到并发胜者。
        when(documentMapper.selectOne(any())).thenReturn(null, existing);
        when(documentMapper.insert(any(Document.class)))
                .thenThrow(new DuplicateKeyException("uk_doc_space_active_hash"));

        DocumentUploadResponse result = service.upload(1L, 10L, markdown("notes.md", "# TLB"));

        assertThat(result.duplicate()).isTrue();
        assertThat(result.document().id()).isEqualTo(55L);
        verify(documentMapper).insert(any(Document.class));
        assertThat(regularFiles()).isEmpty();
    }

    @Test
    void databaseFailureDeletesAlreadyStoredFile() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectOne(any())).thenReturn(null);
        when(documentMapper.insert(any(Document.class)))
                .thenThrow(new RuntimeException("db down"));

        Throwable error = catchThrowable(() ->
                service.upload(1L, 10L, markdown("notes.md", "# TLB")));

        assertThat(error).isInstanceOf(RuntimeException.class).hasMessage("db down");
        assertThat(regularFiles()).isEmpty();
    }

    @Test
    void otherUsersSpaceIsRejectedBeforeReadingFile() {
        when(spaceService.ownedSpace(2L, 10L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() -> service.upload(2L, 10L, markdown("notes.md", "# TLB")))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(documentMapper);
        assertThat(regularFiles()).isEmpty();
    }

    @Test
    void listsDocumentsOfOwnedSpace() {
        Document existing = existingDocument(55L);
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectList(any())).thenReturn(List.of(existing));

        List<DocumentResponse> result = service.listBySpace(1L, 10L);

        assertThat(result).hasSize(1);
        assertThat(result.get(0).id()).isEqualTo(55L);
        assertThat(result.get(0).status()).isEqualTo(DocumentStatus.UPLOADING);
    }

    @Test
    void getRejectsOtherUsersDocumentWithoutLeakingExistence() {
        when(documentMapper.selectById(55L)).thenReturn(existingDocument(55L));

        assertThatThrownBy(() -> service.get(2L, 55L))
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(404);
                    assertThat(ex.getMessage()).isEqualTo("文档不存在");
                });

        verifyNoInteractions(spaceService);
    }

    @Test
    void getRejectsDocumentWhoseSpaceIsNoLongerOwned() {
        when(documentMapper.selectById(55L)).thenReturn(existingDocument(55L));
        when(spaceService.ownedSpace(1L, 10L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() -> service.get(1L, 55L))
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(404);
                    assertThat(ex.getMessage()).isEqualTo("文档不存在");
                });
    }

    @Test
    void getReturnsOwnedDocument() {
        Document existing = existingDocument(55L);
        when(documentMapper.selectById(55L)).thenReturn(existing);
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        DocumentResponse result = service.get(1L, 55L);

        assertThat(result.id()).isEqualTo(55L);
        assertThat(result.fileName()).isEqualTo("old.md");
    }

    @Test
    void missingOrSoftDeletedDocumentIsNotFound() {
        when(documentMapper.selectById(55L)).thenReturn(null);

        assertThatThrownBy(() -> service.get(1L, 55L))
                .isInstanceOf(BusinessException.class)
                .hasMessage("文档不存在");
    }

    @Test
    void readyIdsChecksOwnershipThenDelegatesWithTrimmedIdentity() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        ReadyDocument ready = new ReadyDocument();
        ready.setId(7L);
        ready.setIndexVersion(3);
        when(documentMapper.selectReadyDocuments(
                1L, 10L, "text-embedding-v4", "hash", 256, "v1:abc", false))
                .thenReturn(List.of(ready));

        List<ReadyDocument> result = service.listReadyDocuments(
                1L, 10L, "  text-embedding-v4  ", " hash ", 256, " v1:abc ");

        assertThat(result).containsExactly(ready);
        assertThat(result.get(0).getIndexVersion()).isEqualTo(3);
        verify(spaceService).ownedSpace(1L, 10L);
        verify(documentMapper).selectReadyDocuments(
                1L, 10L, "text-embedding-v4", "hash", 256, "v1:abc", false);
    }

    @Test
    void readyIdsWithoutModelDoesNotFilterByModel() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectReadyDocuments(1L, 10L, null, null, null, null, false))
                .thenReturn(List.of(new ReadyDocument()));

        assertThat(service.listReadyDocuments(1L, 10L, "  ", null, null, null)).hasSize(1);

        verify(documentMapper).selectReadyDocuments(1L, 10L, null, null, null, null, false);
    }

    // ===================== RAG-04 严格指纹模式 =====================

    @Test
    void strictModePassesStrictFlagAndStillObservesLegacyDocuments() {
        DocumentService strictService = serviceWith(new RetrievalProperties(true));
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp", true))
                .thenReturn(List.of(new ReadyDocument()));
        when(documentMapper.selectLegacyReadySummary(1L, 10L)).thenReturn(List.of());

        List<ReadyDocument> result = strictService.listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp");

        assertThat(result).hasSize(1);
        verify(documentMapper).selectReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp", true);
        verify(documentMapper).selectLegacyReadySummary(1L, 10L);
    }

    @Test
    void strictModeRejectsModelOnlyNormalRetrievalBeforeQuerying() {
        DocumentService strictService = serviceWith(new RetrievalProperties(true));
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() -> strictService.listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, null))
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(400);
                    assertThat(ex.getMessage()).contains("embeddingFingerprint");
                });

        verifyNoInteractions(documentMapper);
    }

    @Test
    void strictModeRejectsBlankFingerprintWithoutDowngrading() {
        DocumentService strictService = serviceWith(new RetrievalProperties(true));
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() -> strictService.listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "   "))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void strictModeRejectsModelMissingEvenWithFingerprint() {
        DocumentService strictService = serviceWith(new RetrievalProperties(true));
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() -> strictService.listReadyDocuments(
                1L, 10L, null, "openai", 1024, "v1:fp"))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void observationFailureNeverBreaksRetrieval() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        ReadyDocument ready = new ReadyDocument();
        ready.setId(7L);
        when(documentMapper.selectReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp", false))
                .thenReturn(List.of(ready));
        when(documentMapper.selectLegacyReadySummary(1L, 10L))
                .thenThrow(new RuntimeException("summary query down"));

        assertThat(service.listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp"))
                .containsExactly(ready);
    }

    @Test
    void observationAggregatesLegacyModelsAndCollectionsIncludingNullCollection() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp", false))
                .thenReturn(List.of());
        when(documentMapper.selectLegacyReadySummary(1L, 10L)).thenReturn(List.of(
                summary("text-embedding-v4", "old_collection", 2),
                summary("text-embedding-v4", null, 1),
                summary("hash-v1", "hash_collection", 1)));

        // 观测只打日志，但必须真的读取聚合结果；这里通过不抛异常 + 完成查询验证。
        assertThat(service.listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai", 1024, "v1:fp")).isEmpty();
        verify(documentMapper).selectLegacyReadySummary(1L, 10L);
    }

    @Test
    void legacyDiagnosticsIsOwnerScoped() {
        LegacyReadyDocument item = new LegacyReadyDocument();
        item.setId(5L);
        item.setUserId(1L);
        item.setLearningSpaceId(10L);
        item.setEmbeddingModel("text-embedding-v4");
        item.setVectorCollection(null);
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectLegacyReadyDocuments(1L, 10L, "text-embedding-v4"))
                .thenReturn(List.of(item));

        LegacyReadyReport report =
                service.legacyReadyDiagnostics(1L, 10L, " text-embedding-v4 ");

        assertThat(report.legacyReadyDocs()).isEqualTo(1);
        assertThat(report.legacyModels()).containsExactly("text-embedding-v4");
        assertThat(report.legacyCollections()).containsExactly("<null>");
        assertThat(report.items()).containsExactly(item);
        verify(documentMapper).selectLegacyReadyDocuments(1L, 10L, "text-embedding-v4");
    }

    @Test
    void legacyDiagnosticsRejectsOtherUsersSpaceWith404() {
        when(spaceService.ownedSpace(2L, 10L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() -> service.legacyReadyDiagnostics(2L, 10L, null))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void readyIdsRejectsOverlongModelBeforeQuerying() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() ->
                service.listReadyDocuments(1L, 10L, "m".repeat(129), null, null, null))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void readyIdsRejectsNonPositiveDimension() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() ->
                service.listReadyDocuments(1L, 10L, "hash-v1", null, 0, null))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void readyIdsRejectsFingerprintWithoutModel() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() ->
                service.listReadyDocuments(1L, 10L, null, null, null, "v1:abc"))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void readyIdsRejectsOtherUsersSpace() {
        when(spaceService.ownedSpace(2L, 10L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() ->
                service.listReadyDocuments(2L, 10L, "hash-v1", null, null, null))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(documentMapper);
    }

    // ===================== helpers =====================

    private DocumentService serviceWith(RetrievalProperties properties) {
        return new DocumentService(
                documentMapper, spaceService, storageService, indexService, deletionService,
                properties);
    }

    private static LegacyReadySummary summary(
            String model, String collection, int documentCount) {
        LegacyReadySummary row = new LegacyReadySummary();
        row.setEmbeddingModel(model);
        row.setVectorCollection(collection);
        row.setDocumentCount(documentCount);
        return row;
    }

    private static MockMultipartFile markdown(String name, String content) {
        return new MockMultipartFile("file", name, "text/markdown",
                content.getBytes(StandardCharsets.UTF_8));
    }

    private static Document existingDocument(long id) {
        Document document = new Document();
        document.setId(id);
        document.setLearningSpaceId(10L);
        document.setUserId(1L);
        document.setName("old");
        document.setFileName("old.md");
        document.setFileType("md");
        document.setSize(3L);
        document.setContentHash("a".repeat(64));
        document.setStatus(DocumentStatus.UPLOADING);
        document.setChunkCount(0);
        document.setIndexAttempts(0);
        document.setIndexVersion(0);
        return document;
    }

    private List<Path> regularFiles() {
        try (var paths = Files.walk(root)) {
            return paths.filter(Files::isRegularFile).toList();
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
