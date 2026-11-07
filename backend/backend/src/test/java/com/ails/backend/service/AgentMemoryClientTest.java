package com.ails.backend.service;

import com.ails.backend.dto.memory.agent.MemorySyncRequest;
import com.ails.backend.dto.memory.agent.MemorySyncResponse;
import com.ails.backend.dto.memory.agent.MemoryVectorHit;
import com.ails.backend.dto.memory.agent.MemoryVectorSearchRequest;
import com.ails.backend.security.InternalIdentitySigner;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * {@link AgentMemoryClient} 的契约测试。
 *
 * <p>用 JDK 自带的 {@link HttpServer} 起一个假 Agent，验证：</p>
 * <ol>
 *   <li>发出去的 JSON 字段名与 Agent 的 camelCase 契约一致；</li>
 *   <li>内部 token / 签名身份 / 用户头都随请求发送；</li>
 *   <li>4xx 被标记为永久失败，5xx / 超时 / 契约错误可重试。</li>
 * </ol>
 */
class AgentMemoryClientTest {

    private static final String TEST_TOKEN = "test-internal-token";
    private static final String TEST_IDENTITY_SECRET = "test-identity-secret";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final List<HttpServer> servers = new ArrayList<>();
    private final AtomicReference<String> lastRequestBody = new AtomicReference<>("");
    private final AtomicReference<String> lastToken = new AtomicReference<>("");
    private final AtomicReference<String> lastUser = new AtomicReference<>("");
    private final AtomicReference<String> lastIdentity = new AtomicReference<>("");
    private final AtomicInteger requestCount = new AtomicInteger();

    @AfterEach
    void stopServers() {
        servers.forEach(server -> server.stop(0));
        servers.clear();
    }

    @Test
    @DisplayName("UPSERT：请求体为 camelCase，且携带内部 token / 签名身份 / 用户头")
    void syncUpsertSendsCamelCaseContract() throws Exception {
        HttpServer server = startServer(200,
                "{\"code\":0,\"data\":{\"pointId\":\"p-42\",\"collection\":\"ails_memories_hash-v1_8\",\"skipped\":false}}");

        MemorySyncResponse response = client(server, TEST_TOKEN).sync(new MemorySyncRequest(
                9001L, "UPSERT", 42L, 7L, "用户偏好用递归解释分治", "hash-42", 3,
                "SPACE", 9L, null, "LEARNING", 0.8f, "ACTIVE"));

        assertThat(response.pointId()).isEqualTo("p-42");
        assertThat(response.collection()).isEqualTo("ails_memories_hash-v1_8");
        assertThat(response.skipped()).isFalse();

        JsonNode body = MAPPER.readTree(lastRequestBody.get());
        assertThat(body.path("eventId").asLong()).isEqualTo(9001L);
        assertThat(body.path("operation").asText()).isEqualTo("UPSERT");
        assertThat(body.path("memoryId").asLong()).isEqualTo(42L);
        assertThat(body.path("userId").asLong()).isEqualTo(7L);
        assertThat(body.path("content").asText()).isEqualTo("用户偏好用递归解释分治");
        assertThat(body.path("contentHash").asText()).isEqualTo("hash-42");
        assertThat(body.path("memoryVersion").asInt()).isEqualTo(3);
        assertThat(body.path("scopeType").asText()).isEqualTo("SPACE");
        assertThat(body.path("learningSpaceId").asLong()).isEqualTo(9L);
        assertThat(body.path("memoryType").asText()).isEqualTo("LEARNING");
        assertThat(body.path("importance").asDouble()).isEqualTo(0.8);
        assertThat(body.path("status").asText()).isEqualTo("ACTIVE");

        assertThat(lastToken.get()).isEqualTo(TEST_TOKEN);
        assertThat(lastUser.get()).isEqualTo("7");
        assertThat(lastIdentity.get()).isNotBlank();
        assertThat(requestCount.get()).isEqualTo(1);
    }

    @Test
    @DisplayName("DELETE：只依赖 memoryId/userId，允许 pointId 为 null")
    void syncDeleteAcceptsNullPointId() throws Exception {
        HttpServer server = startServer(200,
                "{\"code\":0,\"data\":{\"pointId\":null,\"collection\":\"c\",\"skipped\":false}}");

        MemorySyncResponse response = client(server, TEST_TOKEN).sync(new MemorySyncRequest(
                null, "DELETE", 42L, 7L, null, null, null,
                null, null, null, null, null, null));

        assertThat(response.pointId()).isNull();
        JsonNode body = MAPPER.readTree(lastRequestBody.get());
        assertThat(body.path("operation").asText()).isEqualTo("DELETE");
        assertThat(body.path("content").isNull()).isTrue();
    }

    @Test
    @DisplayName("search：请求携带 query/userId/recallK，响应只解析 id、分数与 hash")
    void searchParsesHits() throws Exception {
        HttpServer server = startServer(200,
                "{\"code\":0,\"data\":{\"collection\":\"c\",\"warnings\":[],"
                        + "\"hits\":[{\"memoryId\":42,\"score\":0.75,\"contentHash\":\"h42\"},"
                        + "{\"memoryId\":43,\"score\":0.5,\"contentHash\":null}]}}");

        List<MemoryVectorHit> hits = client(server, TEST_TOKEN).search(
                new MemoryVectorSearchRequest("req-1", 7L, 9L, 5L, "递归", 5));

        assertThat(hits).containsExactly(
                new MemoryVectorHit(42L, 0.75, "h42"),
                new MemoryVectorHit(43L, 0.5, null));

        JsonNode body = MAPPER.readTree(lastRequestBody.get());
        assertThat(body.path("requestId").asText()).isEqualTo("req-1");
        assertThat(body.path("userId").asLong()).isEqualTo(7L);
        assertThat(body.path("spaceId").asLong()).isEqualTo(9L);
        assertThat(body.path("conceptId").asLong()).isEqualTo(5L);
        assertThat(body.path("query").asText()).isEqualTo("递归");
        assertThat(body.path("recallK").asInt()).isEqualTo(5);
    }

    @Test
    @DisplayName("Agent 返回 401：映射为永久失败")
    void unauthorizedIsPermanent() throws Exception {
        HttpServer server = startServer(401, "{\"detail\":\"内部凭据无效\"}");

        assertThatThrownBy(() -> client(server, TEST_TOKEN).sync(deleteRequest()))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(401);
                    assertThat(e.isPermanent()).isTrue();
                });
    }

    @Test
    @DisplayName("Agent 返回 500：可重试")
    void serverErrorIsRetryable() throws Exception {
        HttpServer server = startServer(500, "{\"code\":500,\"message\":\"boom\"}");

        assertThatThrownBy(() -> client(server, TEST_TOKEN).sync(deleteRequest()))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(500);
                    assertThat(e.isPermanent()).isFalse();
                });
    }

    @Test
    @DisplayName("HTTP 200 但 code != 0：按 code 映射，并保留永久/可重试语义")
    void businessErrorCodeIsMapped() throws Exception {
        HttpServer server = startServer(200, "{\"code\":422,\"message\":\"非法 operation\"}");

        assertThatThrownBy(() -> client(server, TEST_TOKEN).sync(deleteRequest()))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(422);
                    assertThat(e.isPermanent()).isTrue();
                    assertThat(e.getMessage()).contains("非法 operation");
                });
    }

    @Test
    @DisplayName("响应不是合法 JSON：契约错误 502，标记为永久失败")
    void malformedJsonIsContractError() throws Exception {
        HttpServer server = startServer(200, "not-json");

        assertThatThrownBy(() -> client(server, TEST_TOKEN).sync(deleteRequest()))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(502);
                    assertThat(e.isPermanent()).isTrue();
                });
    }

    @Test
    @DisplayName("响应缺少 hits：契约错误 502，不能当成“没有结果”")
    void missingHitsIsContractError() throws Exception {
        HttpServer server = startServer(200,
                "{\"code\":0,\"data\":{\"collection\":\"c\"}}");

        assertThatThrownBy(() -> client(server, TEST_TOKEN).search(
                new MemoryVectorSearchRequest("r", 7L, null, null, "递归", 5)))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(502);
                    assertThat(e.isPermanent()).isTrue();
                    assertThat(e.getMessage()).contains("hits");
                });
    }

    @Test
    @DisplayName("未配置内部 token：fail closed 503，且不发起请求")
    void missingTokenFailsClosed() throws Exception {
        HttpServer server = startServer(200, "{\"code\":0,\"data\":{}}");

        assertThatThrownBy(() -> client(server, "").sync(deleteRequest()))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(503);
                    assertThat(e.getMessage()).contains("ails.internal.token");
                });
        assertThat(requestCount.get()).isZero();
    }

    @Test
    @DisplayName("请求本身非法：422，且不发起请求")
    void invalidRequestFailsBeforeNetwork() throws Exception {
        HttpServer server = startServer(200, "{\"code\":0,\"data\":{}}");
        AgentMemoryClient client = client(server, TEST_TOKEN);

        assertThatThrownBy(() -> client.search(
                new MemoryVectorSearchRequest("r", 7L, null, null, "   ", 5)))
                .isInstanceOfSatisfying(AgentMemoryException.class, e ->
                        assertThat(e.statusCode()).isEqualTo(422));
        assertThatThrownBy(() -> client.sync(
                new MemorySyncRequest(null, "UPSERT", 42L, 7L, null, null, null,
                        null, null, null, null, null, null)))
                .isInstanceOfSatisfying(AgentMemoryException.class, e ->
                        assertThat(e.statusCode()).isEqualTo(422));
        assertThat(requestCount.get()).isZero();
    }

    @Test
    @DisplayName("Agent 未启动：连接失败映射为 503，可重试")
    void connectionFailureIsRetryable() throws Exception {
        int deadPort;
        try (ServerSocket socket = new ServerSocket(0)) {
            deadPort = socket.getLocalPort();
        }
        AgentMemoryClient client = new AgentMemoryClient(
                "http://127.0.0.1:" + deadPort, 300, TEST_TOKEN, 1000, 1000, signer());

        assertThatThrownBy(() -> client.sync(deleteRequest()))
                .isInstanceOfSatisfying(AgentMemoryException.class, e -> {
                    assertThat(e.statusCode()).isEqualTo(503);
                    assertThat(e.isPermanent()).isFalse();
                });
    }

    // ===================== helpers =====================

    private static MemorySyncRequest deleteRequest() {
        return new MemorySyncRequest(null, "DELETE", 42L, 7L, null, null, null,
                null, null, null, null, null, null);
    }

    private static InternalIdentitySigner signer() {
        return new InternalIdentitySigner(
                TEST_IDENTITY_SECRET, TEST_IDENTITY_SECRET + "-delegation",
                "learning-agent", 60, "learning-backend", 300, 5, 900, 3600);
    }

    private AgentMemoryClient client(HttpServer server, String token) {
        return new AgentMemoryClient(
                "http://127.0.0.1:" + server.getAddress().getPort(),
                1000, token, 2000, 1000, signer());
    }

    private HttpServer startServer(int status, String body) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            requestCount.incrementAndGet();
            lastRequestBody.set(new String(
                    exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            lastToken.set(exchange.getRequestHeaders().getFirst(AgentMemoryClient.TOKEN_HEADER));
            lastUser.set(exchange.getRequestHeaders().getFirst(AgentMemoryClient.USER_HEADER));
            lastIdentity.set(exchange.getRequestHeaders()
                    .getFirst(AgentMemoryClient.IDENTITY_HEADER));

            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        });
        server.start();
        servers.add(server);
        return server;
    }
}
