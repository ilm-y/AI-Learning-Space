package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexConfig;
import com.ails.backend.config.DocumentStorageProperties;
import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.ails.backend.security.InternalIdentitySigner;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
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
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Comparator;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.fail;

/**
 * Spring Boot ↔ 真实 FastAPI ↔ 真实 Qdrant 的跨语言端到端测试（默认跳过）。
 *
 * <p>启用前先启动 Agent 与 Qdrant，例如：</p>
 * <pre>
 * export AILS_INTERNAL_TOKEN=e2e-token
 * export AILS_QDRANT_COLLECTION_PREFIX=ails_e2e_xxx
 * export AILS_EMBEDDING_PROVIDER=hash
 * export AILS_EMBEDDING_DIMENSION=64
 * python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
 *
 * AILS_DOCUMENT_E2E_IT=true \
 * AILS_E2E_AGENT_BASE_URL=http://127.0.0.1:8010 \
 * AILS_E2E_INTERNAL_TOKEN=e2e-token \
 * AILS_E2E_QDRANT_URL=http://127.0.0.1:6333 \
 * AILS_E2E_QDRANT_COLLECTION=ails_e2e_xxx_hash-v1_64 \
 * AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dtest=DocumentIndexE2EIntegrationTest test
 * </pre>
 *
 * <p>验证：真实文件字节经 multipart 到 FastAPI，解析结果落 MySQL，Embedding 后写入
 * Qdrant，最后 READY；重复索引后 DB chunk 数与 Qdrant point 数仍然一一对应。
 * 测试结束后删除临时 Qdrant collection。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_DOCUMENT_E2E_IT", matches = "true")
@SpringBootTest(
        classes = DocumentIndexE2EIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql",
                "ails.document.index.core-pool-size=1",
                "ails.document.index.max-pool-size=1",
                "ails.document.index.queue-capacity=4",
                "ails.document.index.parse-timeout-ms=15000",
                "ails.document.index.index-timeout-ms=30000"
        })
class DocumentIndexE2EIntegrationTest {

    private static final String AGENT_URL = env("AILS_E2E_AGENT_BASE_URL", "http://127.0.0.1:8010");
    private static final String INTERNAL_TOKEN = requiredEnv("AILS_E2E_INTERNAL_TOKEN");
    private static final String QDRANT_URL = env("AILS_E2E_QDRANT_URL", "http://127.0.0.1:6333");
    private static final String QDRANT_COLLECTION = requiredEnv("AILS_E2E_QDRANT_COLLECTION");
    private static final Path STORAGE_ROOT = createStorageRoot();
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Autowired
    private DocumentIndexService indexService;

    @Autowired
    private DocumentChunkService chunkService;

    @Autowired
    private DocumentMapper documentMapper;

    @Autowired
    private JdbcTemplate jdbc;

    private Long userId;
    private Long spaceId;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_document_e2e_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
        registry.add("ails.document.storage.root", STORAGE_ROOT::toString);
        registry.add("ails.document.storage.max-size-bytes", () -> 1024 * 1024);
        registry.add("ails.agent.base-url", () -> AGENT_URL);
        registry.add("ails.internal.token", () -> INTERNAL_TOKEN);
        // AgentDocumentClient 对每次调用都签发 X-Internal-Identity；未配置密钥会 fail closed。
        // 测试进程与 Agent 必须使用同一把密钥。
        registry.add("ails.internal.identity-secret",
                () -> requiredEnv("AILS_E2E_IDENTITY_SECRET"));
    }

    @BeforeEach
    void setUp() throws IOException {
        jdbc.update("DELETE FROM document");
        jdbc.update("DELETE FROM learning_space");
        jdbc.update("DELETE FROM user");
        deleteChildren(STORAGE_ROOT);

        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "e2e-it", "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "e2e-it");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "e2e-it-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
    }

    @AfterAll
    static void tearDown() throws Exception {
        deleteQdrantCollection();
        deleteRecursively(STORAGE_ROOT);
    }

    @Test
    void realAgentAndQdrantIndexAreConsistentAndIdempotent() throws Exception {
        String markdown = "# A\n\nalpha paragraph\n\n## B\n\nbeta paragraph";
        Document document = insertDocument(markdown);

        indexService.submit(document.getId());
        Document ready = awaitStatus(document.getId(), DocumentStatus.READY, Duration.ofSeconds(30));

        assertThat(ready.getChunkCount()).isEqualTo(2);
        assertThat(ready.getEmbeddingModel()).isEqualTo("hash-v1");
        assertThat(ready.getParserVersion()).isEqualTo("document-parser-v1");
        assertThat(ready.getIndexAttempts()).isEqualTo(1);
        assertThat(ready.getIndexVersion()).isEqualTo(1);

        awaitConsistency(document.getId(), 2, Duration.ofSeconds(10));

        // 模拟重试：重新走完整链路，确定性 point ID 覆盖旧点，数量不翻倍。
        jdbc.update("UPDATE document SET status = 'FAILED' WHERE id = ?", document.getId());
        indexService.submit(document.getId());
        Document retried = awaitStatus(document.getId(), DocumentStatus.READY, Duration.ofSeconds(30));

        assertThat(retried.getIndexAttempts()).isEqualTo(2);
        assertThat(retried.getIndexVersion()).isEqualTo(2);
        // READY 是 complete 的提交点，旧版本清理在其后异步执行；
        // 这里等待清理完成后再断言最终只保留活跃版本，断言强度不变。
        awaitConsistency(document.getId(), 2, Duration.ofSeconds(10));
    }

    /**
     * 有界等待“DB chunk 数 + Qdrant point 数都收敛到 expectedCount”。
     * 只等待最终一致，不降低断言：超时后仍由 {@link #assertConsistency} 抛出真实差异。
     */
    private void awaitConsistency(Long documentId, int expectedCount, Duration timeout)
            throws Exception {
        long deadline = System.nanoTime() + timeout.toNanos();
        AssertionError last = null;
        while (System.nanoTime() < deadline) {
            try {
                assertConsistency(documentId, expectedCount);
                return;
            } catch (AssertionError e) {
                last = e;
                Thread.sleep(100);
            }
        }
        if (last != null) {
            throw last;
        }
        assertConsistency(documentId, expectedCount);
    }

    private void assertConsistency(Long documentId, int expectedCount) throws Exception {
        int dbChunks = chunkService.listByDocument(documentId).size();
        int qdrantPoints = qdrantPointCount(documentId);

        assertThat(dbChunks).isEqualTo(expectedCount);
        assertThat(qdrantPoints).isEqualTo(expectedCount);
        assertThat(dbChunks).isEqualTo(qdrantPoints);
        for (DocumentChunk chunk : chunkService.listByDocument(documentId)) {
            assertThat(chunk.getVectorRef()).isNotBlank();
        }
    }

    private int qdrantPointCount(Long documentId) throws Exception {
        String body = "{\"filter\":{\"must\":[{\"key\":\"document_id\","
                + "\"match\":{\"value\":" + documentId + "}}]},\"exact\":true}";
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(QDRANT_URL + "/collections/" + QDRANT_COLLECTION + "/points/count"))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = HttpClient.newHttpClient()
                .send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertThat(response.statusCode()).isEqualTo(200);
        JsonNode root = MAPPER.readTree(response.body());
        return root.path("result").path("count").asInt(-1);
    }

    private static void deleteQdrantCollection() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(QDRANT_URL + "/collections/" + QDRANT_COLLECTION))
                .timeout(Duration.ofSeconds(10))
                .DELETE()
                .build();
        HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.discarding());
    }

    private Document insertDocument(String content) throws IOException {
        String fileName = "e2e-" + System.nanoTime() + ".md";
        Path relative = Path.of("space-" + spaceId).resolve(fileName);
        Path target = STORAGE_ROOT.resolve(relative);
        Files.createDirectories(target.getParent());
        Files.writeString(target, content, StandardCharsets.UTF_8);

        Document document = new Document();
        document.setLearningSpaceId(spaceId);
        document.setUserId(userId);
        document.setContentHash(Integer.toHexString((content + fileName).hashCode()).repeat(4));
        document.setName("e2e");
        document.setFileName(fileName);
        document.setStorageUri(relative.toString().replace('\\', '/'));
        document.setFileType("md");
        document.setSize((long) content.getBytes(StandardCharsets.UTF_8).length);
        document.setStatus(DocumentStatus.UPLOADING);
        document.setChunkCount(0);
        document.setIndexAttempts(0);
        document.setIndexVersion(0);
        assertThat(documentMapper.insert(document)).isEqualTo(1);
        return document;
    }

    private Document awaitStatus(Long documentId, DocumentStatus expected, Duration timeout)
            throws InterruptedException {
        long deadline = System.nanoTime() + timeout.toNanos();
        Document last = null;
        while (System.nanoTime() < deadline) {
            last = documentMapper.selectById(documentId);
            if (last != null && last.getStatus() == expected) {
                return last;
            }
            Thread.sleep(100);
        }
        fail("文档未进入 " + expected + "，最后状态="
                + (last == null ? "missing" : last.getStatus() + ", error=" + last.getIndexError()));
        return last;
    }

    private static Path createStorageRoot() {
        try {
            return Files.createTempDirectory("ails-document-e2e-it-");
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
                    .forEach(DocumentIndexE2EIntegrationTest::deleteQuietly);
        }
    }

    private static void deleteRecursively(Path root) throws IOException {
        if (!Files.exists(root)) {
            return;
        }
        try (var paths = Files.walk(root)) {
            paths.sorted(Comparator.reverseOrder())
                    .forEach(DocumentIndexE2EIntegrationTest::deleteQuietly);
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

    private static String requiredEnv(String key) {
        String value = System.getenv(key);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("端到端测试缺少环境变量 " + key);
        }
        return value;
    }

    @SpringBootConfiguration
    @EnableAutoConfiguration(exclude = {
            SecurityAutoConfiguration.class,
            SecurityFilterAutoConfiguration.class,
            UserDetailsServiceAutoConfiguration.class
    })
    @EnableConfigurationProperties(DocumentStorageProperties.class)
    @Import({
            DocumentIndexService.class,
            DocumentChunkService.class,
            DocumentStorageService.class,
            AgentDocumentClient.class,
            InternalIdentitySigner.class,
            DocumentIndexConfig.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }
}
