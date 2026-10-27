package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.config.DocumentStorageProperties;
import com.ails.backend.dto.document.DocumentResponse;
import com.ails.backend.dto.document.DocumentUploadResponse;
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

    @TempDir
    Path root;

    private DocumentStorageService storageService;
    private DocumentService service;
    private LearningSpace space;

    @BeforeEach
    void setUp() {
        storageService = new DocumentStorageService(
                new DocumentStorageProperties(root.toString(), 1024 * 1024));
        service = new DocumentService(documentMapper, spaceService, storageService, indexService);

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
    void readyIdsChecksOwnershipThenDelegatesWithTrimmedModel() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectReadyIndexedIds(1L, 10L, "text-embedding-v4"))
                .thenReturn(List.of(7L, 8L));

        List<Long> result = service.listReadyDocumentIds(1L, 10L, "  text-embedding-v4  ");

        assertThat(result).containsExactly(7L, 8L);
        verify(spaceService).ownedSpace(1L, 10L);
        verify(documentMapper).selectReadyIndexedIds(1L, 10L, "text-embedding-v4");
    }

    @Test
    void readyIdsWithoutModelDoesNotFilterByModel() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);
        when(documentMapper.selectReadyIndexedIds(1L, 10L, null)).thenReturn(List.of(7L));

        assertThat(service.listReadyDocumentIds(1L, 10L, "  ")).containsExactly(7L);

        verify(documentMapper).selectReadyIndexedIds(1L, 10L, null);
    }

    @Test
    void readyIdsRejectsOverlongModelBeforeQuerying() {
        when(spaceService.ownedSpace(1L, 10L)).thenReturn(space);

        assertThatThrownBy(() ->
                service.listReadyDocumentIds(1L, 10L, "m".repeat(129)))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(400));

        verifyNoInteractions(documentMapper);
    }

    @Test
    void readyIdsRejectsOtherUsersSpace() {
        when(spaceService.ownedSpace(2L, 10L))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        assertThatThrownBy(() -> service.listReadyDocumentIds(2L, 10L, "hash-v1"))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        verifyNoInteractions(documentMapper);
    }

    // ===================== helpers =====================

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
