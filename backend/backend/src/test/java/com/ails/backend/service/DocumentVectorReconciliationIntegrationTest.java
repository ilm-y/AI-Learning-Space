package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.config.DocumentReconciliationProperties;
import com.ails.backend.config.MyBatisMetaObjectHandler;
import com.ails.backend.config.MybatisPlusConfig;
import com.ails.backend.entity.Document;
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
import java.sql.Timestamp;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RAG-02 / RAG-03 的真实集成测试：真实 MySQL + 真实 Qdrant + 假 Agent HTTP。
 *
 * <p>假 Agent 不做内存模拟，而是把 {@code /vector-count} 与 {@code /delete}
 * 转发到真实 Qdrant。因此“旧点确实从旧 collection 删除”断言的是 Qdrant 中的
 * point 数，而不是 mock 交互。</p>
 *
 * <p>启用方式：</p>
 * <pre>
 * AILS_DOCUMENT_RECONCILE_IT=true AILS_IT_MYSQL_PASSWORD=... \
 *   ./mvnw -Dtest=DocumentVectorReconciliationIntegrationTest test
 * </pre>
 */
@EnabledIfEnvironmentVariable(named = "AILS_DOCUMENT_RECONCILE_IT", matches = "true")
@SpringBootTest(
        classes = DocumentVectorReconciliationIntegrationTest.TestApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
                "spring.sql.init.mode=always",
                "spring.sql.init.schema-locations=classpath:schema.sql",
                "ails.document.index.parse-timeout-ms=5000",
                "ails.document.index.index-timeout-ms=5000",
                "ails.agent.connect-timeout-ms=2000",
                "ails.document.reconcile.enabled=true",
                // 测试直接调用 reconcileOnce()，不让定时器在后台插入一轮。
                "ails.document.reconcile.initial-delay-ms=3600000",
                "ails.document.reconcile.interval-ms=3600000",
                "ails.document.reconcile.batch-size=100",
                "ails.document.reconcile.lease-ttl-ms=600000"
        })
class DocumentVectorReconciliationIntegrationTest {

    private static final String INTERNAL_TOKEN = "reconcile-it-token";
    private static final String QDRANT_URL = env("AILS_QDRANT_URL", "http://127.0.0.1:6333");
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HttpClient QDRANT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(3)).build();
    private static final FakeAgent AGENT = FakeAgent.start(INTERNAL_TOKEN, QDRANT_URL);

    @Autowired
    private DocumentVectorReconciliationService reconciliationService;

    @Autowired
    private DocumentIndexVersionService versionService;

    @Autowired
    private DocumentChunkService chunkService;

    @Autowired
    private DocumentMapper documentMapper;

    @Autowired
    private JdbcTemplate jdbc;

    private Long userId;
    private Long spaceId;
    private final List<String> collections = new ArrayList<>();

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_document_reconcile_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
        registry.add("ails.agent.base-url", AGENT::baseUrl);
        registry.add("ails.internal.token", () -> INTERNAL_TOKEN);
        registry.add("ails.internal.identity-secret", () -> "reconcile-it-identity-secret");
    }

    @BeforeEach
    void setUp() {
        jdbc.update("DELETE FROM document");
        jdbc.update("DELETE FROM learning_space");
        jdbc.update("DELETE FROM user");
        jdbc.update("DELETE FROM scheduled_task_lease");
        AGENT.reset();

        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", "reconcile-it", "x");
        userId = jdbc.queryForObject(
                "SELECT id FROM user WHERE username = ?", Long.class, "reconcile-it");
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                userId, "reconcile-it-space");
        spaceId = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, userId);
    }

    @AfterEach
    void dropCollections() {
        for (String collection : collections) {
            try {
                qdrant("DELETE", "/collections/" + collection, null);
            } catch (Exception ignored) {
                // 清理失败不掩盖主断言。
            }
        }
        collections.clear();
    }

    @AfterAll
    static void tearDown() {
        AGENT.stop();
    }

    // ===================== RAG-02 =====================

    @Test
    void cleanupDeletesEachStaleVersionFromTheCollectionItWasWrittenTo() throws Exception {
        String oldA = newCollection();
        String oldB = newCollection();
        String active = newCollection();

        long documentId = insertDocument(3, active, 2);
        insertChunk(documentId, 1, 0);
        insertChunk(documentId, 2, 0);
        insertChunk(documentId, 2, 1);
        insertChunk(documentId, 3, 0);
        insertChunk(documentId, 3, 1);
        versionService.record(documentId, 1, oldA);
        versionService.record(documentId, 2, oldB);

        upsertPoints(oldA, documentId, 1, 1);
        upsertPoints(oldB, documentId, 2, 2);
        upsertPoints(active, documentId, 3, 2);

        Document document = documentMapper.selectById(documentId);
        List<Integer> cleaned = versionService.purgeStaleVersions(document, List.of(1, 2));

        assertThat(cleaned).containsExactlyInAnyOrder(1, 2);
        // 真实 Qdrant：旧 collection 的 points 被删除，活跃 collection 原样保留。
        assertThat(pointCount(oldA, documentId, 1)).isZero();
        assertThat(pointCount(oldB, documentId, 2)).isZero();
        assertThat(pointCount(active, documentId, 3)).isEqualTo(2);
        // 删除请求必须携带各版本自己的 collection，而不是活跃 collection。
        assertThat(AGENT.deletedCollections()).containsExactlyInAnyOrder(oldA, oldB);
        assertThat(AGENT.deletedCollections()).doesNotContain(active);

        assertThat(chunkService.listByDocument(documentId)).hasSize(2);
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM document_chunk WHERE document_id=? AND index_version IN (1,2)",
                Integer.class, documentId)).isZero();
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM document_index_version WHERE document_id=?",
                Integer.class, documentId)).isZero();
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM document_chunk WHERE document_id=? AND index_version=3",
                Integer.class, documentId)).isEqualTo(2);
    }

    @Test
    void cleanupGroupsVersionsThatShareOneCollection() throws Exception {
        String shared = newCollection();
        String active = newCollection();

        long documentId = insertDocument(3, active, 1);
        insertChunk(documentId, 1, 0);
        insertChunk(documentId, 2, 0);
        insertChunk(documentId, 3, 0);
        versionService.record(documentId, 1, shared);
        versionService.record(documentId, 2, shared);

        upsertPoints(shared, documentId, 1, 1);
        upsertPoints(shared, documentId, 2, 1);
        upsertPoints(active, documentId, 3, 1);

        versionService.purgeStaleVersions(documentMapper.selectById(documentId), List.of(1, 2));

        assertThat(pointCount(shared, documentId, 1)).isZero();
        assertThat(pointCount(shared, documentId, 2)).isZero();
        assertThat(pointCount(active, documentId, 3)).isEqualTo(1);
        // 同 collection 的版本合并成一次删除请求。
        assertThat(AGENT.deleteRequests).hasSize(1);
        assertThat(AGENT.deleteRequests.get(0).path("indexVersions").toString())
                .isEqualTo("[1,2]");
    }

    @Test
    void legacyVersionWithoutLocationIsLeftInQdrantAndNeverGuessed() throws Exception {
        String legacy = newCollection();
        String active = newCollection();

        long documentId = insertDocument(1, active, 1);
        insertChunk(documentId, 0, 0);
        insertChunk(documentId, 1, 0);
        upsertLegacyPoints(legacy, documentId, 1);
        upsertPoints(active, documentId, 1, 1);

        versionService.purgeStaleVersions(documentMapper.selectById(documentId), List.of(0));

        // 没有版本记录 → 不猜 collection、不发删除请求；MySQL 记录删除，Qdrant 残留。
        assertThat(AGENT.deleteRequests).isEmpty();
        assertThat(legacyPointCount(legacy, documentId)).isEqualTo(1);
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM document_chunk WHERE document_id=? AND index_version=0",
                Integer.class, documentId)).isZero();
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM document_chunk WHERE document_id=? AND index_version=1",
                Integer.class, documentId)).isEqualTo(1);
    }

    // ===================== RAG-03 =====================

    @Test
    void consistentDocumentsRecordOkAndKeepReadyStatus() throws Exception {
        String collection = newCollection();
        long documentId = insertDocument(2, collection, 2);
        insertChunk(documentId, 2, 0);
        insertChunk(documentId, 2, 1);
        versionService.record(documentId, 2, collection);
        upsertPoints(collection, documentId, 2, 2);

        assertThat(reconciliationService.reconcileOnce()).isZero();

        Map<String, Object> row = documentRow(documentId);
        assertThat(row.get("status")).isEqualTo("READY");
        assertThat(row.get("vector_reconciliation_status")).isEqualTo("OK");
        assertThat((String) row.get("vector_reconciliation_detail"))
                .contains("expected=2", "actual=2", "collection=" + collection);
        assertThat(row.get("index_error")).isNull();
    }

    @Test
    void zeroPointsIsMismatchWithoutTouchingDocumentStatus() throws Exception {
        String collection = newCollection();
        long documentId = insertDocument(2, collection, 2);
        insertChunk(documentId, 2, 0);
        insertChunk(documentId, 2, 1);
        versionService.record(documentId, 2, collection);

        assertThat(reconciliationService.reconcileOnce()).isEqualTo(1);

        Map<String, Object> row = documentRow(documentId);
        assertThat(row.get("vector_reconciliation_status")).isEqualTo("MISMATCH");
        assertThat((String) row.get("vector_reconciliation_detail"))
                .contains("expected=2", "actual=0");
        // 对账不修改文档状态，也不写 index_error。
        assertThat(row.get("status")).isEqualTo("READY");
        assertThat(row.get("index_error")).isNull();
    }

    @Test
    void partialLossReportsExactCounts() throws Exception {
        String collection = newCollection();
        long documentId = insertDocument(2, collection, 3);
        for (int i = 0; i < 3; i++) {
            insertChunk(documentId, 2, i);
        }
        versionService.record(documentId, 2, collection);
        upsertPoints(collection, documentId, 2, 2);

        assertThat(reconciliationService.reconcileOnce()).isEqualTo(1);

        Map<String, Object> row = documentRow(documentId);
        assertThat(row.get("vector_reconciliation_status")).isEqualTo("MISMATCH");
        assertThat((String) row.get("vector_reconciliation_detail"))
                .contains("expected=3", "actual=2");
        assertThat(row.get("status")).isEqualTo("READY");
    }

    @Test
    void missingCollectionIsReachableMismatch() throws Exception {
        String missing = "ails_reconcile_it_missing_" + UUID.randomUUID().toString().replace("-", "");
        long documentId = insertDocument(2, missing, 1);
        insertChunk(documentId, 2, 0);
        versionService.record(documentId, 2, missing);

        assertThat(reconciliationService.reconcileOnce()).isEqualTo(1);

        Map<String, Object> row = documentRow(documentId);
        assertThat(row.get("vector_reconciliation_status")).isEqualTo("MISMATCH");
        assertThat((String) row.get("vector_reconciliation_detail"))
                .contains("actual=0", "collection does not exist");
        assertThat(row.get("status")).isEqualTo("READY");
    }

    @Test
    void unreachableQdrantIsUnreachableNotMismatchAndStatusStaysReady() throws Exception {
        String collection = newCollection();
        long documentId = insertDocument(2, collection, 1);
        insertChunk(documentId, 2, 0);
        versionService.record(documentId, 2, collection);
        AGENT.countStatus = 503;

        assertThat(reconciliationService.reconcileOnce()).isZero();

        Map<String, Object> row = documentRow(documentId);
        assertThat(row.get("vector_reconciliation_status")).isEqualTo("UNREACHABLE");
        assertThat((String) row.get("vector_reconciliation_detail"))
                .contains("expected=1", "actual=<unknown>");
        assertThat(row.get("status")).isEqualTo("READY");
        assertThat(row.get("index_error")).isNull();
    }

    @Test
    void legacyDocumentWithoutCollectionIsUnlocatableAndNotQueried() throws Exception {
        long documentId = insertDocument(1, null, 1);
        insertChunk(documentId, 1, 0);
        int callsBefore = AGENT.countCalls.get();

        assertThat(reconciliationService.reconcileOnce()).isZero();

        Map<String, Object> row = documentRow(documentId);
        assertThat(row.get("vector_reconciliation_status")).isEqualTo("UNLOCATABLE");
        assertThat((String) row.get("vector_reconciliation_detail"))
                .contains("collection=<unlocatable>");
        assertThat(row.get("status")).isEqualTo("READY");
        assertThat(AGENT.countCalls.get()).isEqualTo(callsBefore);
    }

    @Test
    void rebuildInProgressIsNotReconciled() throws Exception {
        String collection = newCollection();
        long documentId = insertDocument(2, collection, 1);
        insertChunk(documentId, 2, 0);
        versionService.record(documentId, 2, collection);
        jdbc.update("UPDATE document SET index_started_at = NOW() WHERE id = ?", documentId);

        assertThat(reconciliationService.reconcileOnce()).isZero();

        assertThat(documentRow(documentId).get("vector_reconciliation_status")).isNull();
    }

    @Test
    void scanRotatesToLeastRecentlyCheckedDocuments() {
        long checkedOld = insertDocument(1, "rotation-a", 1);
        insertChunk(checkedOld, 1, 0);
        long checkedRecent = insertDocument(1, "rotation-b", 1);
        insertChunk(checkedRecent, 1, 0);
        jdbc.update("UPDATE document SET vector_reconciliation_checked_at = ? WHERE id = ?",
                Timestamp.valueOf(LocalDateTime.now().minusHours(2)), checkedOld);
        jdbc.update("UPDATE document SET vector_reconciliation_checked_at = ? WHERE id = ?",
                Timestamp.valueOf(LocalDateTime.now().minusHours(1)), checkedRecent);

        // batchSize=1：最久未检查的先被选中，而不是固定 id 顺序。
        assertThat(documentMapper.selectReconcilableDocuments(1))
                .extracting(com.ails.backend.dto.document.ReconcilableDocument::id)
                .containsExactly(checkedOld);

        long neverChecked = insertDocument(1, "rotation-c", 1);
        insertChunk(neverChecked, 1, 0);
        assertThat(documentMapper.selectReconcilableDocuments(1))
                .extracting(com.ails.backend.dto.document.ReconcilableDocument::id)
                .containsExactly(neverChecked);
    }

    @Test
    void realDatabaseLeaseBlocksAnotherInstanceAndExpiresForRetry() throws Exception {
        String collection = newCollection();
        long documentId = insertDocument(2, collection, 1);
        insertChunk(documentId, 2, 0);
        versionService.record(documentId, 2, collection);
        upsertPoints(collection, documentId, 2, 1);

        jdbc.update("""
                INSERT INTO scheduled_task_lease (task_name, owner, expires_at)
                VALUES (?, ?, ?)
                """, DocumentVectorReconciliationService.TASK_NAME, "other-instance",
                Timestamp.valueOf(LocalDateTime.now().plusMinutes(30)));
        int callsBefore = AGENT.countCalls.get();

        // 另一个实例持有未过期租约：本轮必须整体跳过，不能扫描/写观测列。
        assertThat(reconciliationService.reconcileOnce()).isZero();
        assertThat(AGENT.countCalls.get()).isEqualTo(callsBefore);
        assertThat(documentRow(documentId).get("vector_reconciliation_status")).isNull();

        // 租约过期后可被抢占，下一轮正常执行。
        jdbc.update("UPDATE scheduled_task_lease SET expires_at = ? WHERE task_name = ?",
                Timestamp.valueOf(LocalDateTime.now().minusMinutes(1)),
                DocumentVectorReconciliationService.TASK_NAME);
        assertThat(reconciliationService.reconcileOnce()).isZero();
        assertThat(documentRow(documentId).get("vector_reconciliation_status")).isEqualTo("OK");
    }

    // ===================== helpers =====================

    private long insertDocument(int activeVersion, String vectorCollection, int chunkCount) {
        jdbc.update("""
                INSERT INTO document
                    (learning_space_id, user_id, name, file_name, storage_uri, file_type, size,
                     status, chunk_count, embedding_model, embedding_provider,
                     embedding_dimension, vector_collection, index_version, indexed_at)
                VALUES (?, ?, 'doc', 'doc.md', 'space/doc.md', 'md', 1,
                        'READY', ?, 'hash-test', 'hash', 4, ?, ?, NOW())
                """, spaceId, userId, chunkCount, vectorCollection, activeVersion);
        return jdbc.queryForObject("SELECT LAST_INSERT_ID()", Long.class);
    }

    private void insertChunk(long documentId, int indexVersion, int chunkIndex) {
        jdbc.update("""
                INSERT INTO document_chunk
                    (document_id, index_version, chunk_index, content, vector_ref)
                VALUES (?, ?, ?, ?, ?)
                """, documentId, indexVersion, chunkIndex, "chunk-" + chunkIndex,
                documentId + ":" + indexVersion + ":" + chunkIndex);
    }

    private Map<String, Object> documentRow(long documentId) {
        return jdbc.queryForMap("""
                SELECT status, index_error, vector_reconciliation_status,
                       vector_reconciliation_detail
                FROM document WHERE id = ?
                """, documentId);
    }

    private String newCollection() throws Exception {
        String name = "ails_reconcile_it_" + UUID.randomUUID().toString().replace("-", "");
        ObjectNode body = MAPPER.createObjectNode();
        body.putObject("vectors").put("size", 4).put("distance", "Cosine");
        qdrant("PUT", "/collections/" + name, MAPPER.writeValueAsString(body));
        collections.add(name);
        return name;
    }

    private static void upsertPoints(String collection, long documentId,
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
            payload.put("chunk_id", documentId * 1000 + version * 10 + i);
            payload.put("index_version", version);
            payload.put("doc_version", documentId + ":" + version);
        }
        qdrant("PUT", "/collections/" + collection + "/points?wait=true",
                MAPPER.writeValueAsString(body));
    }

    private static void upsertLegacyPoints(String collection, long documentId, int count)
            throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        ArrayNode points = body.putArray("points");
        for (int i = 0; i < count; i++) {
            ObjectNode point = points.addObject();
            point.put("id", UUID.randomUUID().toString());
            ArrayNode vector = point.putArray("vector");
            vector.add(0.1).add(0.2).add(0.3).add(0.4);
            ObjectNode payload = point.putObject("payload");
            payload.put("document_id", documentId);
            payload.put("chunk_id", documentId * 1000 + i);
            // 迁移前 points 没有 index_version 字段。
        }
        qdrant("PUT", "/collections/" + collection + "/points?wait=true",
                MAPPER.writeValueAsString(body));
    }

    private static int pointCount(String collection, long documentId, int version)
            throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        ArrayNode must = body.putObject("filter").putArray("must");
        must.addObject().put("key", "document_id").putObject("match").put("value", documentId);
        must.addObject().put("key", "index_version").putObject("match").put("value", version);
        body.put("exact", true);
        return count(collection, body);
    }

    private static int legacyPointCount(String collection, long documentId) throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        ArrayNode must = body.putObject("filter").putArray("must");
        must.addObject().put("key", "document_id").putObject("match").put("value", documentId);
        must.addObject().putObject("is_empty").put("key", "index_version");
        body.put("exact", true);
        return count(collection, body);
    }

    private static int count(String collection, ObjectNode body) throws Exception {
        JsonNode response = MAPPER.readTree(qdrant("POST",
                "/collections/" + collection + "/points/count",
                MAPPER.writeValueAsString(body)));
        return response.path("result").path("count").asInt(-1);
    }

    private static String qdrant(String method, String path, String body) throws Exception {
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

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }

    // ===================== fake agent =====================

    private static final class FakeAgent {
        private final HttpServer server;
        private final ExecutorService executor;
        private final String token;
        private final String qdrantUrl;

        volatile int countStatus = 200;
        final AtomicInteger countCalls = new AtomicInteger();
        final AtomicInteger deleteCalls = new AtomicInteger();
        final List<JsonNode> deleteRequests = new CopyOnWriteArrayList<>();

        private FakeAgent(HttpServer server, ExecutorService executor,
                          String token, String qdrantUrl) {
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
                server.createContext("/agent/documents/vector-count", agent::handleCount);
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
            countStatus = 200;
            countCalls.set(0);
            deleteCalls.set(0);
            deleteRequests.clear();
        }

        List<String> deletedCollections() {
            return deleteRequests.stream()
                    .map(request -> request.path("collection").asText())
                    .toList();
        }

        void stop() {
            server.stop(0);
            executor.shutdownNow();
        }

        private void handleCount(HttpExchange exchange) throws IOException {
            countCalls.incrementAndGet();
            String body = new String(exchange.getRequestBody().readAllBytes(),
                    StandardCharsets.UTF_8);
            if (!authorized(exchange)) {
                write(exchange, 401, "{\"code\":401,\"message\":\"bad token\"}");
                return;
            }
            if (countStatus != 200) {
                write(exchange, countStatus,
                        "{\"code\":" + countStatus + ",\"message\":\"qdrant unavailable\"}");
                return;
            }
            try {
                JsonNode request = MAPPER.readTree(body);
                String collection = request.path("collection").asText();
                long documentId = request.path("documentId").asLong();
                int indexVersion = request.path("indexVersion").asInt();
                boolean exists = collectionExistsFor(collection);
                long count = exists ? countFor(collection, documentId, indexVersion) : 0;

                ObjectNode data = MAPPER.createObjectNode();
                data.put("documentId", documentId);
                data.put("collection", collection);
                data.put("indexVersion", indexVersion);
                data.put("collectionExists", exists);
                data.put("count", count);
                write(exchange, 200, MAPPER.writeValueAsString(
                        MAPPER.createObjectNode().put("code", 0).set("data", data)));
            } catch (Exception e) {
                write(exchange, 500, "{\"code\":500,\"message\":\"count failed\"}");
            }
        }

        private void handleDelete(HttpExchange exchange) throws IOException {
            deleteCalls.incrementAndGet();
            String body = new String(exchange.getRequestBody().readAllBytes(),
                    StandardCharsets.UTF_8);
            if (!authorized(exchange)) {
                write(exchange, 401, "{\"code\":401,\"message\":\"bad token\"}");
                return;
            }
            try {
                JsonNode request = MAPPER.readTree(body);
                deleteRequests.add(request);
                String collection = request.path("collection").asText();
                long documentId = request.path("documentId").asLong();
                List<Integer> versions = new ArrayList<>();
                request.path("indexVersions").forEach(node -> versions.add(node.asInt()));
                deletePoints(collection, documentId, versions,
                        request.path("deleteLegacy").asBoolean(false));

                ObjectNode data = MAPPER.createObjectNode();
                data.put("documentId", documentId);
                data.put("collection", collection);
                write(exchange, 200, MAPPER.writeValueAsString(
                        MAPPER.createObjectNode().put("code", 0).set("data", data)));
            } catch (Exception e) {
                write(exchange, 500, "{\"code\":500,\"message\":\"delete failed\"}");
            }
        }

        private boolean collectionExistsFor(String collection) throws Exception {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(qdrantUrl + "/collections/" + collection))
                    .timeout(Duration.ofSeconds(3)).GET().build();
            HttpResponse<String> response = HttpClient.newHttpClient().send(request,
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            return response.statusCode() == 200;
        }

        private long countFor(String collection, long documentId, int indexVersion)
                throws Exception {
            ObjectNode body = MAPPER.createObjectNode();
            ArrayNode must = body.putObject("filter").putArray("must");
            must.addObject().put("key", "document_id")
                    .putObject("match").put("value", documentId);
            must.addObject().put("key", "index_version")
                    .putObject("match").put("value", indexVersion);
            body.put("exact", true);
            JsonNode response = MAPPER.readTree(forward("POST",
                    "/collections/" + collection + "/points/count",
                    MAPPER.writeValueAsString(body)));
            return response.path("result").path("count").asLong();
        }

        private void deletePoints(String collection, long documentId,
                                  List<Integer> versions, boolean deleteLegacy)
                throws Exception {
            ObjectNode body = MAPPER.createObjectNode();
            ObjectNode filter = body.putObject("filter");
            filter.putArray("must").addObject().put("key", "document_id")
                    .putObject("match").put("value", documentId);
            ArrayNode should = filter.putArray("should");
            if (!versions.isEmpty()) {
                ArrayNode any = should.addObject().put("key", "index_version")
                        .putObject("match").putArray("any");
                versions.forEach(any::add);
            }
            if (deleteLegacy) {
                should.addObject().putObject("is_empty").put("key", "index_version");
            }
            forward("POST", "/collections/" + collection + "/points/delete?wait=true",
                    MAPPER.writeValueAsString(body));
        }

        private String forward(String method, String path, String body) throws Exception {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(qdrantUrl + path))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                    .build();
            HttpResponse<String> response = HttpClient.newHttpClient().send(request,
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() >= 300) {
                throw new IllegalStateException("Qdrant " + method + " " + path
                        + " 失败: HTTP " + response.statusCode() + " " + response.body());
            }
            return response.body();
        }

        private boolean authorized(HttpExchange exchange) {
            String provided = exchange.getRequestHeaders().getFirst("X-Internal-Token");
            return token.equals(provided);
        }

        private static void write(HttpExchange exchange, int status, String body)
                throws IOException {
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        }
    }

    // ===================== test application =====================

    @SpringBootConfiguration
    @EnableAutoConfiguration(exclude = {
            SecurityAutoConfiguration.class,
            SecurityFilterAutoConfiguration.class,
            UserDetailsServiceAutoConfiguration.class
    })
    @EnableConfigurationProperties({DocumentIndexProperties.class,
            DocumentReconciliationProperties.class})
    @Import({
            DocumentIndexVersionService.class,
            DocumentChunkService.class,
            AgentDocumentClient.class,
            InternalIdentitySigner.class,
            DocumentVectorReconciliationService.class,
            MybatisPlusConfig.class,
            MyBatisMetaObjectHandler.class
    })
    static class TestApplication {
    }
}
