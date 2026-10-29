package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.config.DocumentStorageProperties;
import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.SecurityAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.SecurityFilterAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.UserDetailsServiceAutoConfiguration;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 真实 MySQL 集成测试（默认跳过）。
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_DOCUMENT_IT=true \
 * AILS_IT_MYSQL_PASSWORD=... \
 * ./mvnw -Dtest=DocumentServiceIntegrationTest test
 * </pre>
 *
 * <p>使用独立数据库 {@code ails_document_it}，不会连接或修改 {@code ails} 开发库。
 * 测试上下文只加载数据层、{@link DocumentService}、{@link LearningSpaceService} 与
 * {@link DocumentStorageService}，刻意不加载 {@code SecurityConfig} —— 后者目前存在
 * 一个与本轮无关的过滤器注册顺序问题，修它超出本轮修改范围。</p>
 *
 * <p>覆盖：真实唯一键并发去重、{@code @TableLogic} 软删过滤、软删后重新上传、
 * 跨 Space 隔离、Space 所有权、真实文件落盘与上传状态。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_DOCUMENT_IT", matches = "true")
@SpringBootTest(
        classes = DocumentServiceIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql"
        })
class DocumentServiceIntegrationTest {

    private static final Path STORAGE_ROOT = createStorageRoot();

    @Autowired
    private DocumentService documentService;

    @Autowired
    private DocumentMapper documentMapper;

    @Autowired
    private JdbcTemplate jdbc;

    /**
     * 本测试只验证上传/去重/软删与状态初始值；索引链路由 DocumentIndexService
     * 的独立测试覆盖，因此这里替换掉真实 Agent 调用。
     */
    @MockBean
    private DocumentIndexService indexService;

    private Long userId1;
    private Long userId2;
    private Long spaceId1;
    private Long spaceId2;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_document_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
        registry.add("ails.document.storage.root", STORAGE_ROOT::toString);
        registry.add("ails.document.storage.max-size-bytes", () -> 1024 * 1024);
    }

    @BeforeEach
    void setUp() throws IOException {
        jdbc.update("DELETE FROM document");
        jdbc.update("DELETE FROM learning_space");
        jdbc.update("DELETE FROM user");
        deleteChildren(STORAGE_ROOT);

        userId1 = insertUser("it-user-1");
        userId2 = insertUser("it-user-2");
        spaceId1 = insertSpace(userId1, "it-space-1");
        spaceId2 = insertSpace(userId1, "it-space-2");
    }

    @AfterAll
    static void cleanUp() throws IOException {
        deleteRecursively(STORAGE_ROOT);
    }

    @Test
    void listAndDetailRespectOwnership() {
        DocumentUploadResponse uploaded = documentService.upload(
                userId1, spaceId1, file("notes.md", "# TLB"));

        assertThat(documentService.listBySpace(userId1, spaceId1)).hasSize(1);
        Document document = documentMapper.selectById(uploaded.document().id());
        assertThat(document).isNotNull();
        assertThat(document.getStatus()).isEqualTo(DocumentStatus.UPLOADING);
        assertThat(document.getFileName()).isEqualTo("notes.md");
        assertThat(document.getLearningSpaceId()).isEqualTo(spaceId1);
        assertThat(document.getUserId()).isEqualTo(userId1);
        assertThat(document.getChunkCount()).isZero();
        assertThat(document.getIndexVersion()).isZero();
        assertThat(document.getStorageUri()).matches("space-" + spaceId1 + "/[0-9a-f]{32}\\.md");
        assertThat(STORAGE_ROOT.resolve(document.getStorageUri())).exists();

        assertThat(documentService.get(userId1, uploaded.document().id()).id())
                .isEqualTo(uploaded.document().id());

        // 他人文档：统一 404，不泄露文档是否存在
        assertThatThrownBy(() -> documentService.get(userId2, uploaded.document().id()))
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(404);
                    assertThat(ex.getMessage()).isEqualTo("文档不存在");
                });
        assertThatThrownBy(() -> documentService.listBySpace(userId2, spaceId1))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));
    }

    @Test
    void duplicateUploadReturnsExistingDocumentAndLeavesOneFile() {
        DocumentUploadResponse first = documentService.upload(userId1, spaceId1, file("same.md", "same"));
        DocumentUploadResponse second = documentService.upload(userId1, spaceId1, file("same.md", "same"));

        assertThat(first.duplicate()).isFalse();
        assertThat(second.duplicate()).isTrue();
        assertThat(second.document().id()).isEqualTo(first.document().id());
        assertThat(documentCount()).isEqualTo(1);
        assertThat(storedFileCount()).isEqualTo(1);
    }

    @Test
    void sameContentInDifferentSpacesIsAllowed() {
        DocumentUploadResponse a = documentService.upload(userId1, spaceId1, file("same.md", "same"));
        DocumentUploadResponse b = documentService.upload(userId1, spaceId2, file("same.md", "same"));

        assertThat(a.duplicate()).isFalse();
        assertThat(b.duplicate()).isFalse();
        assertThat(a.document().id()).isNotEqualTo(b.document().id());
        assertThat(documentCount()).isEqualTo(2);
        assertThat(storedFileCount()).isEqualTo(2);
    }

    @Test
    void softDeletedDocumentIsHiddenAndItsHashCanBeReused() {
        DocumentUploadResponse first = documentService.upload(userId1, spaceId1, file("old.md", "same"));
        documentMapper.deleteById(first.document().id());

        assertThat(documentService.listBySpace(userId1, spaceId1)).isEmpty();
        assertThatThrownBy(() -> documentService.get(userId1, first.document().id()))
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(404);
                    assertThat(ex.getMessage()).isEqualTo("文档不存在");
                });

        DocumentUploadResponse second = documentService.upload(userId1, spaceId1, file("new.md", "same"));

        assertThat(second.duplicate()).isFalse();
        assertThat(second.document().id()).isNotEqualTo(first.document().id());
        assertThat(documentCount()).isEqualTo(2);
        assertThat(activeDocumentCount()).isEqualTo(1);
    }

    @Test
    void readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace() {
        long ready = insertDocument(spaceId1, userId1, "READY", 2, true, "text-embedding-v4", false);
        insertChunk(ready, 0, "point-ready-0");
        insertChunk(ready, 1, "point-ready-1");

        long deleting = insertDocument(spaceId1, userId1, "DELETING", 1, true, "text-embedding-v4", false);
        insertChunk(deleting, 0, "point-deleting");

        long deleted = insertDocument(spaceId1, userId1, "DELETED", 1, true, "text-embedding-v4", true);
        insertChunk(deleted, 0, "point-deleted");

        long processing = insertDocument(spaceId1, userId1, "PROCESSING", 1, false, null, false);
        insertChunk(processing, 0, "point-processing");

        long readyWithoutIndexedAt =
                insertDocument(spaceId1, userId1, "READY", 1, false, "text-embedding-v4", false);
        insertChunk(readyWithoutIndexedAt, 0, "point-no-indexed-at");

        long incomplete =
                insertDocument(spaceId1, userId1, "READY", 2, true, "text-embedding-v4", false);
        insertChunk(incomplete, 0, "point-incomplete-0");
        insertChunk(incomplete, 1, null); // 声明 2 块，但只有 1 块真正写入了向量

        insertDocument(spaceId1, userId1, "READY", 0, true, "text-embedding-v4", false);

        long noModel = insertDocument(spaceId1, userId1, "READY", 1, true, null, false);
        insertChunk(noModel, 0, "point-no-model");

        // Qdrant 里可能还有残留，但 MySQL 归属/状态已经不允许：
        long wrongUser = insertDocument(spaceId1, userId2, "READY", 1, true, "text-embedding-v4", false);
        insertChunk(wrongUser, 0, "point-wrong-user");

        long otherSpace =
                insertDocument(spaceId2, userId1, "READY", 1, true, "text-embedding-v4", false);
        insertChunk(otherSpace, 0, "point-other-space");

        assertThat(documentService.listReadyDocumentIds(userId1, spaceId1, "text-embedding-v4"))
                .containsExactly(ready);

        // 模型不匹配（旧 Hash 索引）时一个都不返回，迫使重新索引。
        assertThat(documentService.listReadyDocumentIds(userId1, spaceId1, "hash-v1")).isEmpty();

        // 不传模型是管理/排查视图，仍只返回该 Space 内权限与状态都合法的文档。
        assertThat(documentService.listReadyDocumentIds(userId1, spaceId1, null))
                .containsExactly(ready);

        assertThatThrownBy(() ->
                documentService.listReadyDocumentIds(userId2, spaceId1, "text-embedding-v4"))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));
    }

    @Test
    void otherUsersSpaceCannotBeUploaded() {
        assertThatThrownBy(() -> documentService.upload(userId2, spaceId1, file("x.md", "x")))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(404));

        assertThat(documentCount()).isZero();
        assertThat(storedFileCount()).isZero();
    }

    @Test
    void concurrentUploadsCreateExactlyOneActiveDocument() throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch ready = new CountDownLatch(2);
        CountDownLatch start = new CountDownLatch(1);
        Callable<DocumentUploadResponse> task = () -> {
            ready.countDown();
            if (!start.await(5, TimeUnit.SECONDS)) {
                throw new IllegalStateException("并发测试启动超时");
            }
            return documentService.upload(userId1, spaceId1, file("race.md", "race-content"));
        };

        Future<DocumentUploadResponse> first = pool.submit(task);
        Future<DocumentUploadResponse> second = pool.submit(task);
        try {
            assertThat(ready.await(5, TimeUnit.SECONDS)).isTrue();
            start.countDown();

            DocumentUploadResponse a = first.get(15, TimeUnit.SECONDS);
            DocumentUploadResponse b = second.get(15, TimeUnit.SECONDS);

            assertThat(a.document().id()).isEqualTo(b.document().id());
            assertThat(a.duplicate()).isNotEqualTo(b.duplicate());
            assertThat(documentCount()).isEqualTo(1);
            assertThat(activeDocumentCount()).isEqualTo(1);
            assertThat(storedFileCount()).isEqualTo(1);
        } finally {
            pool.shutdownNow();
        }
    }

    // ===================== test application =====================

    /** 只加载本测试需要的最小 Spring 上下文，避开与文档无关的 Web/Security 装配。 */
    @SpringBootConfiguration
    @EnableAutoConfiguration(exclude = {
            SecurityAutoConfiguration.class,
            SecurityFilterAutoConfiguration.class,
            UserDetailsServiceAutoConfiguration.class
    })
    @EnableConfigurationProperties(DocumentStorageProperties.class)
    @Import({
            DocumentService.class,
            LearningSpaceService.class,
            DocumentStorageService.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }

    // ===================== helpers =====================

    private static MockMultipartFile file(String name, String content) {
        return new MockMultipartFile("file", name, "text/markdown",
                content.getBytes(StandardCharsets.UTF_8));
    }

    private Long insertUser(String username) {
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", username, "x");
        return jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, username);
    }

    private Long insertSpace(Long ownerId, String name) {
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                ownerId, name);
        // 测试库顺序执行，没有并发建空间；取该 owner 最新一行即可。
        return jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    /**
     * 直接造一条指定状态的文档。content_hash 保持 NULL，避免 active hash 唯一键
     * 干扰「同一 Space 内多条测试文档」的场景；这些行只用于白名单 SQL 测试。
     */
    private long insertDocument(
            Long spaceId,
            Long ownerId,
            String status,
            int chunkCount,
            boolean indexed,
            String embeddingModel,
            boolean deleted) {
        jdbc.update("""
                        INSERT INTO document
                            (learning_space_id, user_id, name, file_name, storage_uri,
                             status, chunk_count, embedding_model, indexed_at, deleted_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                spaceId,
                ownerId,
                "it-document",
                "it-document.md",
                "space-" + spaceId + "/it-document.md",
                status,
                chunkCount,
                embeddingModel,
                indexed ? new java.sql.Timestamp(System.currentTimeMillis()) : null,
                deleted ? new java.sql.Timestamp(System.currentTimeMillis()) : null);
        Long id = jdbc.queryForObject(
                "SELECT id FROM document ORDER BY id DESC LIMIT 1", Long.class);
        if (id == null) {
            throw new IllegalStateException("插入测试文档失败");
        }
        return id;
    }

    private void insertChunk(long documentId, int chunkIndex, String vectorRef) {
        jdbc.update("""
                        INSERT INTO document_chunk (document_id, chunk_index, content, vector_ref)
                        VALUES (?, ?, ?, ?)
                        """,
                documentId, chunkIndex, "chunk-" + chunkIndex, vectorRef);
    }

    private int documentCount() {
        Integer count = jdbc.queryForObject("SELECT COUNT(*) FROM document", Integer.class);
        return count == null ? 0 : count;
    }

    private int activeDocumentCount() {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM document WHERE deleted_at IS NULL", Integer.class);
        return count == null ? 0 : count;
    }

    private long storedFileCount() {
        try (var paths = Files.walk(STORAGE_ROOT)) {
            return paths.filter(Files::isRegularFile).count();
        } catch (IOException e) {
            throw new IllegalStateException("统计存储文件失败", e);
        }
    }

    private static Path createStorageRoot() {
        try {
            return Files.createTempDirectory("ails-document-it-");
        } catch (IOException e) {
            throw new IllegalStateException("创建测试存储目录失败", e);
        }
    }

    private static void deleteChildren(Path root) throws IOException {
        if (!Files.exists(root)) {
            return;
        }
        try (var paths = Files.walk(root)) {
            paths.filter(path -> !path.equals(root))
                    .sorted(Comparator.reverseOrder())
                    .forEach(DocumentServiceIntegrationTest::deleteQuietly);
        }
    }

    private static void deleteRecursively(Path root) throws IOException {
        if (!Files.exists(root)) {
            return;
        }
        try (var paths = Files.walk(root)) {
            paths.sorted(Comparator.reverseOrder())
                    .forEach(DocumentServiceIntegrationTest::deleteQuietly);
        }
    }

    private static void deleteQuietly(Path path) {
        try {
            Files.deleteIfExists(path);
        } catch (IOException e) {
            throw new IllegalStateException("清理测试文件失败: " + path, e);
        }
    }

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }
}
