package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexConfig;
import com.ails.backend.config.DocumentStorageProperties;
import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.dto.document.agent.AgentParsedChunk;
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
import org.junit.jupiter.api.AfterEach;
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
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RAG-01 回归：旧 worker 的清理/回滚必须被 attempt fencing 拦住。
 *
 * <p>场景（真实 MySQL + 真实 Qdrant）：</p>
 * <ol>
 *   <li>worker A 持有 attempt 1；租约被回收/超时后，worker B 抢到 attempt 2；</li>
 *   <li>B 成功完成，MySQL 的 {@code index_version=2}，Qdrant 有版本 2 的 points；</li>
 *   <li>A 晚到并恢复执行 {@code cleanupOldVersions}/{@code rollbackStagedVersion}；</li>
 *   <li>断言 A 不会删除 B 的活跃 points/chunk。</li>
 * </ol>
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_DOCUMENT_FENCING_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw.cmd -Dtest=DocumentIndexFencingIntegrationTest test
 * </pre>
 *
 * <p>Qdrant 默认使用 {@code http://127.0.0.1:6333}；测试使用随机临时 collection，
 * 结束后删除。Agent 使用本地假 HTTP 服务，但删除请求会真实转发到 Qdrant，
 * 因此断言的是 Qdrant 中的 points，而不是 mock 交互。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_DOCUMENT_FENCING_IT", matches = "true")
@SpringBootTest(
        classes = DocumentIndexFencingIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql",
                "ails.document.index.core-pool-size=1",
                "ails.document.index.max-pool-size=1",
                "ails.document.index.queue-capacity=4",
                "ails.document.index.parse-timeout-ms=5000",
                "ails.document.index.index-timeout-ms=5000",
                "ails.document.index.recovery-interval-ms=3600000",
                "ails.document.index.recovery-initial-delay-ms=3600000",
                "ails.document.index.cleanup-interval-ms=3600000",
                "ails.document.index.cleanup-initial-delay-ms=3600000"
        })
class DocumentIndexFencingIntegrationTest {

    private static final String INTERNAL_TOKEN = "document-fencing-it-token";
    private static final String QDRANT_URL = env("AILS_QDRANT_URL", "http://127.0.0.1:6333");
    private static final Path STORAGE_ROOT = createStorageRoot();
    private static final FakeAgent AGENT = FakeAgent.start(INTERNAL_TOKEN, QDRANT_URL);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HttpClient QDRANT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

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
    private String collection;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_document_fencing_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
        registry.add("ails.document.storage.root", STORAGE_ROOT::toString);
        registry.add("ails.document.storage.max-size-bytes", () -> 1024 * 1024);
        registry.add("ails.agent.base-url", AGENT::baseUrl);
        registry.add("ails.internal.token", () -> INTERNAL_TOKEN);
        registry.add("ails.internal.identity-secret", () -> "document-fencing-it-identity-secret");
    }

    @BeforeEach
    void setUp() throws Exception {
        jdbc.update("DELETE FROM document_chunk");
        jdbc.update("DELETE FROM document");
        jdbc.update("DELETE FROM learning_space");
        jdbc.update("DELETE FROM user");
        AGENT.reset();

        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "fencing-it", "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "fencing-it");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "fencing-it-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);

        collection = "ails_fencing_it_" + UUID.randomUUID().toString().replace("-", "");
        createCollection(collection);
    }

    @AfterEach
    void tearDown() {
        if (collection != null) {
            deleteCollection(collection);
        }
    }

    @AfterAll
    static void afterAll() throws IOException {
        AGENT.stop();
        deleteRecursively(STORAGE_ROOT);
    }

    @Test
    void staleWorkerCleanupDoesNotDeleteNewWorkersActivePoints() throws Exception {
        Document current = readyDocument(2, 2);
        stageChunks(current, 1);
        stageChunks(current, 2);
        upsertPoints(collection, current.getId(), 1, 2);
        upsertPoints(collection, current.getId(), 2, 2);

        // worker A 的旧快照：attempt=1，active=1；此时 MySQL 已经是 B 的 attempt=2/active=2。
        Document snapshotA = snapshot(current, 1, 1);
        indexService.cleanupOldVersions(snapshotA, 1, 1, 0, collection);

        // A 没有发删除请求；B 的活跃版本仍然完整。
        assertThat(AGENT.deleteCalls.get()).isZero();
        assertThat(pointCount(collection, current.getId(), 2)).isEqualTo(2);
        assertThat(pointCount(collection, current.getId(), 1)).isEqualTo(2);
        assertThat(chunkService.listActiveByDocument(current.getId(), 2)).hasSize(2);
    }

    @Test
    void staleWorkerRollbackDoesNotDeleteNewWorkersActivePoints() throws Exception {
        Document current = readyDocument(2, 2);
        stageChunks(current, 1);
        stageChunks(current, 2);
        upsertPoints(collection, current.getId(), 1, 2);
        upsertPoints(collection, current.getId(), 2, 2);

        Document snapshotA = snapshot(current, 1, 1);
        indexService.rollbackStagedVersion(snapshotA, 1);

        assertThat(AGENT.deleteCalls.get()).isZero();
        assertThat(pointCount(collection, current.getId(), 2)).isEqualTo(2);
        assertThat(pointCount(collection, current.getId(), 1)).isEqualTo(2);
        assertThat(chunkService.listActiveByDocument(current.getId(), 2)).hasSize(2);
    }

    @Test
    void currentWorkerCleanupDeletesOnlyOlderVersions() throws Exception {
        Document current = readyDocument(2, 2);
        stageChunks(current, 1);
        stageChunks(current, 2);
        upsertPoints(collection, current.getId(), 1, 2);
        upsertPoints(collection, current.getId(), 2, 2);

        indexService.cleanupOldVersions(current, 2, 2, 1, collection);

        assertThat(AGENT.deleteCalls.get()).isEqualTo(1);
        assertThat(pointCount(collection, current.getId(), 1)).isZero();
        assertThat(pointCount(collection, current.getId(), 2)).isEqualTo(2);
        assertThat(chunkService.listByDocument(current.getId()))
                .extracting(DocumentChunk::getIndexVersion)
                .containsOnly(2);
    }

    @Test
    void currentWorkerRollbackDeletesOnlyItsOwnAttempt() throws Exception {
        Document current = readyDocument(2, 1);
        current.setIndexStartedAt(LocalDateTime.now());
        documentMapper.updateById(current);
        stageChunks(current, 1);
        stageChunks(current, 2);
        upsertPoints(collection, current.getId(), 1, 2);
        upsertPoints(collection, current.getId(), 2, 2);

        indexService.rollbackStagedVersion(current, 2);

        assertThat(AGENT.deleteCalls.get()).isEqualTo(1);
        assertThat(pointCount(collection, current.getId(), 2)).isZero();
        assertThat(pointCount(collection, current.getId(), 1)).isEqualTo(2);
        assertThat(chunkService.listByDocument(current.getId()))
                .extracting(DocumentChunk::getIndexVersion)
                .containsOnly(1);
    }

    // ===================== helpers =====================

    private Document readyDocument(int attempts, int activeVersion) {
        Document document = new Document();
        document.setLearningSpaceId(spaceId);
        document.setUserId(userId);
        document.setContentHash(UUID.randomUUID().toString().replace("-", "").repeat(2));
        document.setName("fencing-notes");
        document.setFileName("fencing-" + UUID.randomUUID() + ".md");
        document.setStorageUri("space-" + spaceId + "/fencing.md");
        document.setFileType("md");
        document.setSize(10L);
        document.setStatus(DocumentStatus.READY);
        document.setChunkCount(2);
        document.setIndexAttempts(attempts);
        document.setIndexVersion(activeVersion);
        document.setEmbeddingModel("hash-test");
        document.setEmbeddingProvider("hash");
        document.setEmbeddingDimension(4);
        document.setEmbeddingFingerprint("v1:fencing");
        document.setVectorCollection(collection);
        document.setIndexedAt(LocalDateTime.now());
        assertThat(documentMapper.insert(document)).isEqualTo(1);
        return document;
    }

    private void stageChunks(Document document, int version) {
        chunkService.stageChunks(document, List.of(
                new AgentParsedChunk(0, "chunk-0-v" + version, 2, true, Map.of()),
                new AgentParsedChunk(1, "chunk-1-v" + version, 2, true, Map.of())
        ), version);
    }

    private static Document snapshot(Document source, int attempts, int activeVersion) {
        Document snapshot = new Document();
        snapshot.setId(source.getId());
        snapshot.setUserId(source.getUserId());
        snapshot.setVectorCollection(source.getVectorCollection());
        snapshot.setEmbeddingModel(source.getEmbeddingModel());
        snapshot.setEmbeddingProvider(source.getEmbeddingProvider());
        snapshot.setEmbeddingDimension(source.getEmbeddingDimension());
        snapshot.setEmbeddingFingerprint(source.getEmbeddingFingerprint());
        snapshot.setStatus(DocumentStatus.READY);
        snapshot.setIndexAttempts(attempts);
        snapshot.setIndexVersion(activeVersion);
        return snapshot;
    }

    private static void createCollection(String name) throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        body.putObject("vectors").put("size", 4).put("distance", "Cosine");
        send("PUT", "/collections/" + name, MAPPER.writeValueAsString(body));
    }

    private static void deleteCollection(String name) {
        try {
            send("DELETE", "/collections/" + name, null);
        } catch (Exception ignored) {
            // 测试清理失败不掩盖主断言。
        }
    }

    private static void upsertPoints(String collection, Long documentId,
                                     int version, int count) throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        ArrayNode points = body.putArray("points");
        for (int i = 0; i < count; i++) {
            ObjectNode point = points.addObject();
            point.put("id", UUID.randomUUID().toString());
            ArrayNode vector = point.putArray("vector");
            vector.add(0.1).add(0.2).add(0.3).add(0.4);
            ObjectNode payload = point.putObject("payload");
            payload.put("document_id", documentId);
            payload.put("chunk_id", documentId * 100 + version * 10 + i);
            payload.put("index_version", version);
            payload.put("doc_version", documentId + ":" + version);
            payload.put("content", "v" + version + "-chunk-" + i);
        }
        send("PUT", "/collections/" + collection + "/points?wait=true",
                MAPPER.writeValueAsString(body));
    }

    private static int pointCount(String collection, Long documentId, int version) throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        ObjectNode filter = body.putObject("filter");
        ArrayNode must = filter.putArray("must");
        must.addObject().put("key", "document_id")
                .putObject("match").put("value", documentId);
        must.addObject().put("key", "index_version")
                .putObject("match").put("value", version);
        body.put("exact", true);
        JsonNode response = MAPPER.readTree(send("POST",
                "/collections/" + collection + "/points/count",
                MAPPER.writeValueAsString(body)));
        return response.path("result").path("count").asInt();
    }

    private static void deletePointsInQdrant(String collection, long documentId,
                                             List<Integer> versions, boolean deleteLegacy)
            throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        ObjectNode filter = body.putObject("filter");
        filter.putArray("must").addObject().put("key", "document_id")
                .putObject("match").put("value", documentId);
        ArrayNode should = filter.putArray("should");
        if (versions != null && !versions.isEmpty()) {
            ArrayNode any = should.addObject().put("key", "index_version")
                    .putObject("match").putArray("any");
            versions.forEach(any::add);
        }
        if (deleteLegacy) {
            should.addObject().putObject("is_empty").put("key", "index_version");
        }
        send("POST", "/collections/" + collection + "/points/delete?wait=true",
                MAPPER.writeValueAsString(body));
    }

    private static String send(String method, String path, String body) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(QDRANT_URL + path))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json");
        if (body == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            builder.method(method, HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        }
        HttpResponse<String> response = QDRANT.send(builder.build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() >= 300) {
            throw new IllegalStateException("Qdrant " + method + " " + path
                    + " 失败: HTTP " + response.statusCode() + " " + response.body());
        }
        return response.body();
    }

    private static Path createStorageRoot() {
        try {
            return Files.createTempDirectory("ails-document-fencing-it-");
        } catch (IOException e) {
            throw new IllegalStateException("创建测试存储目录失败", e);
        }
    }

    private static void deleteRecursively(Path root) throws IOException {
        if (!Files.exists(root)) {
            return;
        }
        try (var paths = Files.walk(root)) {
            paths.sorted(java.util.Comparator.reverseOrder()).forEach(path -> {
                try {
                    Files.deleteIfExists(path);
                } catch (IOException e) {
                    throw new IllegalStateException("清理测试文件失败: " + path, e);
                }
            });
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

    // ===================== fake agent =====================

    private static final class FakeAgent {
        private final HttpServer server;
        private final ExecutorService executor;
        private final String token;
        private final String qdrantUrl;
        final AtomicInteger deleteCalls = new AtomicInteger();

        private FakeAgent(HttpServer server, ExecutorService executor, String token, String qdrantUrl) {
            this.server = server;
            this.executor = executor;
            this.token = token;
            this.qdrantUrl = qdrantUrl;
        }

        static FakeAgent start(String token, String qdrantUrl) {
            try {
                HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
                ExecutorService executor = Executors.newCachedThreadPool();
                FakeAgent agent = new FakeAgent(server, executor, token, qdrantUrl);
                server.createContext("/agent/documents/delete", agent::handleDelete);
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
            deleteCalls.set(0);
        }

        void stop() {
            server.stop(0);
            executor.shutdownNow();
        }

        private void handleDelete(HttpExchange exchange) throws IOException {
            deleteCalls.incrementAndGet();
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (!token.equals(exchange.getRequestHeaders().getFirst("X-Internal-Token"))) {
                write(exchange, 401, "{\"code\":401,\"message\":\"bad token\"}");
                return;
            }
            JsonNode request = MAPPER.readTree(body);
            long documentId = request.path("documentId").asLong();
            String collection = request.path("collection").asText();
            List<Integer> versions = new ArrayList<>();
            request.path("indexVersions").forEach(node -> versions.add(node.asInt()));
            boolean deleteLegacy = request.path("deleteLegacy").asBoolean(false);
            try {
                deletePointsInQdrant(collection, documentId, versions, deleteLegacy);
            } catch (Exception e) {
                write(exchange, 500, "{\"code\":500,\"message\":\"" + e + "\"}");
                return;
            }
            ObjectNode data = MAPPER.createObjectNode();
            data.put("documentId", documentId);
            data.put("collection", collection);
            write(exchange, 200, MAPPER.writeValueAsString(
                    MAPPER.createObjectNode().put("code", 0).set("data", data)));
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
