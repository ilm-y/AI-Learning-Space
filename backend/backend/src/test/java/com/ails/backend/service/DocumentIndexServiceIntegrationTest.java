package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexConfig;
import com.ails.backend.config.DocumentStorageProperties;
import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.ails.backend.security.InternalIdentitySigner;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
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
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.temporal.ChronoUnit;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.fail;

/**
 * 文档索引编排的真实集成测试（默认跳过）。
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_DOCUMENT_INDEX_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dtest=DocumentIndexServiceIntegrationTest test
 * </pre>
 *
 * <p>使用独立数据库 {@code ails_document_index_it} 与本地假 Agent HTTP 服务，
 * 不连接开发库、不要求 FastAPI/Qdrant 已启动。假 Agent 校验内部请求头并返回
 * 真实 JSON 契约，因此这里同时验证 {@link AgentDocumentClient} 的 multipart /
 * JSON 请求与响应解析。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_DOCUMENT_INDEX_IT", matches = "true")
@SpringBootTest(
        classes = DocumentIndexServiceIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql",
                "ails.document.index.core-pool-size=2",
                "ails.document.index.max-pool-size=2",
                "ails.document.index.queue-capacity=16",
                "ails.document.index.parse-timeout-ms=5000",
                "ails.document.index.index-timeout-ms=5000",
                "ails.document.index.stale-processing-timeout-ms=60000",
                // 定时扫描在测试里不自动跑；每个用例直接调用 recoverStale(now)，
                // 用可控时间点验证阈值边界。
                "ails.document.index.recovery-interval-ms=3600000",
                "ails.document.index.recovery-initial-delay-ms=3600000",
                "ails.document.index.recovery-batch-size=100"
        })
class DocumentIndexServiceIntegrationTest {

    private static final String INTERNAL_TOKEN = "document-index-it-token";
    private static final Path STORAGE_ROOT = createStorageRoot();
    private static final FakeAgent AGENT = FakeAgent.start(INTERNAL_TOKEN);

    @Autowired
    private DocumentIndexService indexService;

    @Autowired
    private DocumentService documentService;

    @Autowired
    private DocumentChunkService chunkService;

    @Autowired
    private DocumentMapper documentMapper;

    @Autowired
    private DocumentIndexRecoveryService recoveryService;

    @Autowired
    private JdbcTemplate jdbc;

    private Long userId;
    private Long spaceId;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_document_index_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
        registry.add("ails.document.storage.root", STORAGE_ROOT::toString);
        registry.add("ails.document.storage.max-size-bytes", () -> 1024 * 1024);
        registry.add("ails.agent.base-url", AGENT::baseUrl);
        registry.add("ails.internal.token", () -> INTERNAL_TOKEN);
        // AgentDocumentClient 会为文档请求签发代表用户的内部声明。
        // 这是测试专用随机值，不是生产密钥。
        registry.add("ails.internal.identity-secret", () -> "document-index-it-identity-secret");
    }

    @BeforeEach
    void setUp() throws IOException {
        jdbc.update("DELETE FROM document");
        jdbc.update("DELETE FROM learning_space");
        jdbc.update("DELETE FROM user");
        deleteChildren(STORAGE_ROOT);
        AGENT.reset();

        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "index-it", "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "index-it");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "index-it-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
    }

    @AfterAll
    static void tearDown() throws IOException {
        AGENT.stop();
        deleteRecursively(STORAGE_ROOT);
    }

    @Test
    void successfulIndexWritesChunksVectorRefsAndReadyState() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);

        indexService.submit(document.getId());
        Document ready = awaitStatus(document.getId(), DocumentStatus.READY);

        assertThat(ready.getChunkCount()).isEqualTo(2);
        assertThat(ready.getIndexAttempts()).isEqualTo(1);
        assertThat(ready.getIndexVersion()).isEqualTo(1);
        assertThat(ready.getParserVersion()).isEqualTo("test-parser-v1");
        assertThat(ready.getEmbeddingModel()).isEqualTo("hash-test");
        assertThat(ready.getIndexedAt()).isNotNull();
        assertThat(ready.getIndexError()).isNull();

        List<DocumentChunk> chunks = chunkService.listByDocument(document.getId());
        assertThat(chunks).hasSize(2);
        assertThat(chunks).extracting(DocumentChunk::getChunkIndex).containsExactly(0, 1);
        assertThat(chunks).allSatisfy(chunk -> {
            assertThat(chunk.getId()).isNotNull();
            assertThat(chunk.getVectorRef()).isNotBlank();
            assertThat(chunk.getMetadataJson()).contains("tokenCountEstimated");
        });
        assertThat(chunks.get(0).getVectorRef()).isNotEqualTo(chunks.get(1).getVectorRef());

        assertThat(AGENT.parseCalls.get()).isEqualTo(1);
        assertThat(AGENT.indexCalls.get()).isEqualTo(1);
        assertThat(AGENT.lastParseBody).contains("hello content");
        assertThat(AGENT.lastIndexBody).contains("\"spaceId\":" + spaceId);
    }

    @Test
    void uploadKicksOffIndexingAndReturnsRealAsyncStatus() throws Exception {
        MockMultipartFile file = new MockMultipartFile(
                "file", "notes.md", "text/markdown",
                "hello content".getBytes(StandardCharsets.UTF_8));

        DocumentUploadResponse upload = documentService.upload(userId, spaceId, file);

        assertThat(upload.duplicate()).isFalse();
        assertThat(upload.document().status())
                .isIn(DocumentStatus.PROCESSING, DocumentStatus.READY);
        assertThat(upload.document().status()).isNotEqualTo(DocumentStatus.FAILED);

        Document ready = awaitStatus(upload.document().id(), DocumentStatus.READY);
        assertThat(ready.getChunkCount()).isEqualTo(2);
        assertThat(ready.getIndexAttempts()).isEqualTo(1);
        assertThat(documentService.listBySpace(userId, spaceId)).hasSize(1);
    }

    @Test
    void parseFailureNeverBecomesReady() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        AGENT.parseStatus = 400;
        AGENT.parseError = "invalid document";

        indexService.submit(document.getId());
        Document failed = awaitStatus(document.getId(), DocumentStatus.FAILED);

        assertThat(failed.getIndexError()).contains("HTTP 400").contains("invalid document");
        assertThat(failed.getChunkCount()).isZero();
        assertThat(failed.getIndexVersion()).isZero();
        assertThat(chunkService.listByDocument(document.getId())).isEmpty();
        assertThat(AGENT.indexCalls.get()).isZero();
    }

    @Test
    void chunkPersistenceFailureRollsBackAllChunksAndFails() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        AGENT.oversizeSecondChunk = true;

        indexService.submit(document.getId());
        Document failed = awaitStatus(document.getId(), DocumentStatus.FAILED);

        // 第二个 chunk 撞唯一键后，第一个 chunk 也必须随事务回滚。
        assertThat(chunkService.listByDocument(document.getId())).isEmpty();
        assertThat(failed.getChunkCount()).isZero();
        assertThat(failed.getIndexError()).isNotBlank();
        assertThat(AGENT.indexCalls.get()).isZero();
    }

    @Test
    void embeddingOrQdrantFailureAfterChunkWriteNeverBecomesReady() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        AGENT.indexStatus = 502;
        AGENT.indexError = "embedding service unavailable";

        indexService.submit(document.getId());
        Document failed = awaitStatus(document.getId(), DocumentStatus.FAILED);

        assertThat(failed.getIndexVersion()).isZero();
        assertThat(failed.getChunkCount()).isZero();
        assertThat(failed.getIndexError()).contains("HTTP 502");

        List<DocumentChunk> chunks = chunkService.listByDocument(document.getId());
        assertThat(chunks).hasSize(2);
        assertThat(chunks).allSatisfy(chunk -> assertThat(chunk.getVectorRef()).isNull());
    }

    @Test
    void malformedVectorMappingFailsAndDoesNotWriteRefs() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        AGENT.dropOneVector = true;

        indexService.submit(document.getId());
        Document failed = awaitStatus(document.getId(), DocumentStatus.FAILED);

        assertThat(failed.getIndexError()).contains("向量引用数量");
        assertThat(chunkService.listByDocument(document.getId()))
                .allSatisfy(chunk -> assertThat(chunk.getVectorRef()).isNull());
    }

    @Test
    void internalAuthFailureNeverBecomesReady() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        AGENT.requiredToken = "wrong-token";

        indexService.submit(document.getId());
        Document failed = awaitStatus(document.getId(), DocumentStatus.FAILED);

        assertThat(failed.getIndexError()).contains("HTTP 401");
        assertThat(chunkService.listByDocument(document.getId())).isEmpty();
    }

    @Test
    void repeatedIndexIsIdempotentAndIncrementsVersionOnlyOnSuccess() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        indexService.submit(document.getId());
        awaitStatus(document.getId(), DocumentStatus.READY);

        jdbc.update("UPDATE document SET status = 'FAILED' WHERE id = ?", document.getId());
        indexService.submit(document.getId());
        Document ready = awaitStatus(document.getId(), DocumentStatus.READY);

        assertThat(ready.getChunkCount()).isEqualTo(2);
        assertThat(ready.getIndexAttempts()).isEqualTo(2);
        assertThat(ready.getIndexVersion()).isEqualTo(2);
        assertThat(chunkService.listByDocument(document.getId())).hasSize(2);
        assertThat(AGENT.parseCalls.get()).isEqualTo(2);
        assertThat(AGENT.indexCalls.get()).isEqualTo(2);
    }

    @Test
    void indexErrorIsTruncatedTo1024Chars() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.UPLOADING);
        assertThat(indexService.markProcessing(document.getId())).isTrue();

        indexService.fail(document.getId(), "x".repeat(5000));

        Document failed = documentMapper.selectById(document.getId());
        assertThat(failed.getStatus()).isEqualTo(DocumentStatus.FAILED);
        assertThat(failed.getIndexError()).hasSize(1024);
    }

    // ===================== 超时 PROCESSING 回收 =====================

    @Test
    void freshProcessingIsNotRecovered() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.PROCESSING);

        int marked = recoveryService.recoverStale(LocalDateTime.now());

        assertThat(marked).isZero();
        Document stillProcessing = documentMapper.selectById(document.getId());
        assertThat(stillProcessing.getStatus()).isEqualTo(DocumentStatus.PROCESSING);
        assertThat(stillProcessing.getIndexError()).isNull();
    }

    @Test
    void staleProcessingIsFailedWithTraceableErrorAndExcludedFromReady() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.PROCESSING);
        ageDocument(document.getId(), 2, "HOUR");

        int marked = recoveryService.recoverStale(LocalDateTime.now());

        assertThat(marked).isEqualTo(1);
        Document failed = documentMapper.selectById(document.getId());
        assertThat(failed.getStatus()).isEqualTo(DocumentStatus.FAILED);
        assertThat(failed.getIndexError())
                .contains("超过")
                .contains("PROCESSING")
                .contains("中断")
                .contains("可重新提交索引");
        // 回收不伪造索引次数，也不产生 chunk。
        assertThat(failed.getIndexAttempts()).isZero();
        assertThat(failed.getChunkCount()).isZero();
        assertThat(chunkService.listByDocument(document.getId())).isEmpty();
        assertThat(documentMapper.selectReadyIndexedIds(userId, spaceId, "hash-test"))
                .doesNotContain(document.getId());
    }

    @Test
    void restartScanRecoversAnInterruptedDocumentToAnExplicitTerminalState() throws Exception {
        // 模拟进程在索引中被杀、重启后定时扫描走到生产入口 recoverStale()。
        Document document = insertDocument("hello content", DocumentStatus.PROCESSING);
        ageDocument(document.getId(), 2, "HOUR");

        recoveryService.recoverStale();

        Document failed = documentMapper.selectById(document.getId());
        assertThat(failed.getStatus()).isEqualTo(DocumentStatus.FAILED);
        assertThat(failed.getIndexError()).contains("中断").contains("可重新提交索引");
        assertThat(documentMapper.selectReadyIndexedIds(userId, spaceId, "hash-test"))
                .doesNotContain(document.getId());
    }

    @Test
    void recoveryIsIdempotentAndDoesNotRewriteTheError() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.PROCESSING);
        ageDocument(document.getId(), 2, "HOUR");

        assertThat(recoveryService.recoverStale(LocalDateTime.now())).isEqualTo(1);
        Document failed = documentMapper.selectById(document.getId());
        String firstError = failed.getIndexError();
        assertThat(firstError).isNotBlank();

        // 第二次扫描：状态已不是 PROCESSING，条件更新 0 行。
        assertThat(recoveryService.recoverStale(LocalDateTime.now())).isZero();
        Document afterSecondScan = documentMapper.selectById(document.getId());
        assertThat(afterSecondScan.getStatus()).isEqualTo(DocumentStatus.FAILED);
        assertThat(afterSecondScan.getIndexError()).isEqualTo(firstError);
        assertThat(afterSecondScan.getIndexAttempts()).isZero();
    }

    @Test
    void recoveryRespectsTheTimeoutBoundary() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.PROCESSING);
        // 阈值为 60 秒：30 秒前启动的任务仍在合法执行窗口内，不能被回收。
        ageDocument(document.getId(), 30, "SECOND");

        assertThat(recoveryService.recoverStale(LocalDateTime.now())).isZero();
        assertThat(documentMapper.selectById(document.getId()).getStatus())
                .isEqualTo(DocumentStatus.PROCESSING);

        ageDocument(document.getId(), 10, "MINUTE");
        assertThat(recoveryService.recoverStale(LocalDateTime.now())).isEqualTo(1);
        assertThat(documentMapper.selectById(document.getId()).getStatus())
                .isEqualTo(DocumentStatus.FAILED);
    }

    @Test
    void lateCompletionCannotOverwriteARecoveredDocument() throws Exception {
        Document document = insertDocument("hello content", DocumentStatus.PROCESSING);
        ageDocument(document.getId(), 2, "HOUR");
        assertThat(recoveryService.recoverStale(LocalDateTime.now())).isEqualTo(1);

        // 模拟原任务晚到：状态已是 FAILED，complete 的条件更新必须失败。
        assertThatThrownBy(() ->
                indexService.complete(document.getId(), 1, "late-parser", "hash-test"))
                .isInstanceOf(DocumentIndexException.class);

        Document afterLateComplete = documentMapper.selectById(document.getId());
        assertThat(afterLateComplete.getStatus()).isEqualTo(DocumentStatus.FAILED);
        assertThat(afterLateComplete.getIndexedAt()).isNull();
        assertThat(afterLateComplete.getChunkCount()).isZero();
    }

    // ===================== helpers =====================

    private void ageDocument(Long documentId, int amount, String unit) {
        // 用 JVM 时钟而不是 MySQL NOW()：生产代码写入的 updated_at 与回收 cutoff
        // 都来自 JVM；若测试库会话时区与 JVM 不同，NOW() 会把边界用例算错。
        ChronoUnit chronoUnit = switch (unit) {
            case "SECOND" -> ChronoUnit.SECONDS;
            case "MINUTE" -> ChronoUnit.MINUTES;
            case "HOUR" -> ChronoUnit.HOURS;
            default -> throw new IllegalArgumentException("unsupported unit: " + unit);
        };
        LocalDateTime aged = LocalDateTime.now().minus(amount, chronoUnit);
        jdbc.update(
                "UPDATE document SET updated_at = ? WHERE id = ?",
                Timestamp.valueOf(aged),
                documentId);
    }

    private Document insertDocument(String content, DocumentStatus status) throws IOException {
        String fileName = "doc-" + System.nanoTime() + ".md";
        Path relative = Path.of("space-" + spaceId).resolve(fileName);
        Path target = STORAGE_ROOT.resolve(relative);
        Files.createDirectories(target.getParent());
        Files.writeString(target, content, StandardCharsets.UTF_8);

        Document document = new Document();
        document.setLearningSpaceId(spaceId);
        document.setUserId(userId);
        document.setContentHash(shaLike(content, fileName));
        document.setName("notes");
        document.setFileName(fileName);
        document.setStorageUri(relative.toString().replace('\\', '/'));
        document.setFileType("md");
        document.setSize((long) content.getBytes(StandardCharsets.UTF_8).length);
        document.setStatus(status);
        document.setChunkCount(0);
        document.setIndexAttempts(0);
        document.setIndexVersion(0);
        assertThat(documentMapper.insert(document)).isEqualTo(1);
        return document;
    }

    private static String shaLike(String content, String fileName) {
        return Integer.toHexString((content + fileName).hashCode()).repeat(4);
    }

    private Document awaitStatus(Long documentId, DocumentStatus expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        Document last = null;
        while (System.nanoTime() < deadline) {
            last = documentMapper.selectById(documentId);
            if (last != null && last.getStatus() == expected) {
                return last;
            }
            Thread.sleep(50);
        }
        fail("文档未进入 " + expected + "，最后状态="
                + (last == null ? "missing" : last.getStatus() + ", error=" + last.getIndexError()));
        return last;
    }

    private static Path createStorageRoot() {
        try {
            return Files.createTempDirectory("ails-document-index-it-");
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
                    .forEach(DocumentIndexServiceIntegrationTest::deleteQuietly);
        }
    }

    private static void deleteRecursively(Path root) throws IOException {
        if (!Files.exists(root)) {
            return;
        }
        try (var paths = Files.walk(root)) {
            paths.sorted(Comparator.reverseOrder())
                    .forEach(DocumentIndexServiceIntegrationTest::deleteQuietly);
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

    // ===================== test application =====================

    @SpringBootConfiguration
    @EnableAutoConfiguration(exclude = {
            SecurityAutoConfiguration.class,
            SecurityFilterAutoConfiguration.class,
            UserDetailsServiceAutoConfiguration.class
    })
    @EnableConfigurationProperties(DocumentStorageProperties.class)
    @Import({
            DocumentService.class,
            DocumentIndexService.class,
            DocumentIndexRecoveryService.class,
            DocumentChunkService.class,
            DocumentStorageService.class,
            AgentDocumentClient.class,
            InternalIdentitySigner.class,
            LearningSpaceService.class,
            DocumentIndexConfig.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }

    // ===================== fake agent =====================

    private static final class FakeAgent {
        private static final ObjectMapper MAPPER = new ObjectMapper();

        private final HttpServer server;
        private final ExecutorService executor;
        private final String token;

        volatile int parseStatus = 200;
        volatile String parseError = "parse failed";
        volatile int indexStatus = 200;
        volatile String indexError = "index failed";
        volatile boolean oversizeSecondChunk = false;
        volatile boolean dropOneVector = false;
        volatile String requiredToken;
        volatile String lastParseBody = "";
        volatile String lastIndexBody = "";
        final AtomicInteger parseCalls = new AtomicInteger();
        final AtomicInteger indexCalls = new AtomicInteger();

        private FakeAgent(HttpServer server, ExecutorService executor, String token) {
            this.server = server;
            this.executor = executor;
            this.token = token;
            this.requiredToken = token;
        }

        static FakeAgent start(String token) {
            try {
                HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
                ExecutorService executor = Executors.newCachedThreadPool();
                FakeAgent agent = new FakeAgent(server, executor, token);
                server.createContext("/agent/documents/parse", agent::handleParse);
                server.createContext("/agent/documents/index", agent::handleIndex);
                server.setExecutor(executor);
                server.start();
                return agent;
            } catch (IOException e) {
                throw new IllegalStateException("启动假 Agent 失败", e);
            }
        }

        String baseUrl() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        void reset() {
            parseStatus = 200;
            parseError = "parse failed";
            indexStatus = 200;
            indexError = "index failed";
            oversizeSecondChunk = false;
            dropOneVector = false;
            requiredToken = token;
            lastParseBody = "";
            lastIndexBody = "";
            parseCalls.set(0);
            indexCalls.set(0);
        }

        void stop() {
            server.stop(0);
            executor.shutdownNow();
        }

        private void handleParse(HttpExchange exchange) throws IOException {
            parseCalls.incrementAndGet();
            lastParseBody = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (!authorized(exchange)) {
                write(exchange, 401, "{\"code\":401,\"message\":\"bad token\"}");
                return;
            }
            if (parseStatus != 200) {
                write(exchange, parseStatus,
                        "{\"code\":" + parseStatus + ",\"message\":\"" + parseError + "\"}");
                return;
            }

            ObjectNode data = MAPPER.createObjectNode();
            data.put("parserVersion", "test-parser-v1");
            ArrayNode chunks = data.putArray("chunks");
            for (int i = 0; i < 2; i++) {
                ObjectNode chunk = chunks.addObject();
                chunk.put("chunkIndex", i);
                // second chunk 超过 MySQL TEXT 上限（65535 bytes），用于验证事务回滚。
                chunk.put("content", oversizeSecondChunk && i == 1
                        ? "x".repeat(70_000)
                        : "# T\n\nhello content " + i);
                chunk.put("tokenCount", 4);
                chunk.put("tokenCountEstimated", true);
                chunk.putObject("metadata").putArray("headingPath").add("T");
            }
            write(exchange, 200, MAPPER.writeValueAsString(
                    MAPPER.createObjectNode().put("code", 0).set("data", data)));
        }

        private void handleIndex(HttpExchange exchange) throws IOException {
            indexCalls.incrementAndGet();
            lastIndexBody = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (!authorized(exchange)) {
                write(exchange, 401, "{\"code\":401,\"message\":\"bad token\"}");
                return;
            }
            if (indexStatus != 200) {
                write(exchange, indexStatus,
                        "{\"code\":" + indexStatus + ",\"message\":\"" + indexError + "\"}");
                return;
            }

            JsonNode request = MAPPER.readTree(lastIndexBody);
            JsonNode chunks = request.path("chunks");
            ObjectNode data = MAPPER.createObjectNode();
            data.put("collection", "it-collection");
            data.put("embeddingModel", "hash-test");
            data.put("embeddingProvider", "hash");
            ArrayNode vectors = data.putArray("vectors");
            int limit = dropOneVector ? chunks.size() - 1 : chunks.size();
            for (int i = 0; i < limit; i++) {
                ObjectNode vector = vectors.addObject();
                vector.put("chunkId", chunks.get(i).path("chunkId").asLong());
                vector.put("pointId", "point-" + chunks.get(i).path("chunkIndex").asInt());
            }
            write(exchange, 200, MAPPER.writeValueAsString(
                    MAPPER.createObjectNode().put("code", 0).set("data", data)));
        }

        private boolean authorized(HttpExchange exchange) {
            String header = exchange.getRequestHeaders().getFirst("X-Internal-Token");
            String user = exchange.getRequestHeaders().getFirst("X-Internal-User-Id");
            return requiredToken.equals(header) && user != null && !user.isBlank();
        }

        private static void write(HttpExchange exchange, int status, String body) throws IOException {
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        }
    }
}
