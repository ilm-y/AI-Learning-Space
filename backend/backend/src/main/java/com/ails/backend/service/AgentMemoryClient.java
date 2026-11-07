package com.ails.backend.service;

import com.ails.backend.dto.memory.agent.MemorySyncRequest;
import com.ails.backend.dto.memory.agent.MemorySyncResponse;
import com.ails.backend.dto.memory.agent.MemoryVectorHit;
import com.ails.backend.dto.memory.agent.MemoryVectorSearchRequest;
import com.ails.backend.security.InternalIdentitySigner;
import com.ails.backend.utils.JsonUtil;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.ConnectException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Spring Boot → Python Agent 的记忆向量客户端。
 *
 * <p>复用现有内部鉴权：{@code X-Internal-Token} 证明调用方是受信服务，
 * {@code X-Internal-Identity} 是 {@link InternalIdentitySigner} 代表
 * {@code userId} 签发的短期声明。缺少 token 或身份密钥时 fail closed，
 * 绝不降级成匿名写入。</p>
 *
 * <p><b>失败语义</b>：任何 HTTP 错误、鉴权失败、响应契约不合法都抛
 * {@link AgentMemoryException}；调用方（Outbox Relay）据此决定重试或 DEAD。</p>
 *
 * <p>本轮只提供客户端，不修改 {@code MemoryService} 的检索主流程，也不实现
 * Relay。</p>
 */
@Slf4j
@Component
public class AgentMemoryClient {

    static final String TOKEN_HEADER = "X-Internal-Token";
    static final String USER_HEADER = "X-Internal-User-Id";
    static final String IDENTITY_HEADER = "X-Internal-Identity";

    private static final String SYNC_PATH = "/agent/memories/sync";
    private static final String SEARCH_PATH = "/agent/memories/search";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final HttpClient client;
    private final String baseUrl;
    private final String internalToken;
    private final InternalIdentitySigner identitySigner;
    private final Duration syncTimeout;
    private final Duration searchTimeout;

    public AgentMemoryClient(
            @Value("${ails.agent.base-url}") String baseUrl,
            @Value("${ails.agent.connect-timeout-ms:3000}") long connectTimeoutMs,
            @Value("${ails.internal.token:}") String internalToken,
            @Value("${ails.agent.memory-sync-timeout-ms:5000}") long syncTimeoutMs,
            @Value("${ails.agent.memory-search-timeout-ms:2000}") long searchTimeoutMs,
            InternalIdentitySigner identitySigner) {
        this.baseUrl = trimTrailingSlash(baseUrl);
        this.internalToken = internalToken == null ? "" : internalToken.trim();
        this.identitySigner = identitySigner;
        this.syncTimeout = Duration.ofMillis(positive(syncTimeoutMs, 5000));
        this.searchTimeout = Duration.ofMillis(positive(searchTimeoutMs, 2000));
        this.client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(positive(connectTimeoutMs, 3000)))
                .followRedirects(HttpClient.Redirect.NEVER)
                // 与现有 Agent 客户端一致：明文 HTTP 下钉死 1.1，避免 h2c 探测丢请求体。
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    /**
     * 同步一条记忆向量。UPSERT 会重新嵌入；DELETE 只按 memoryId 删除，且幂等。
     */
    public MemorySyncResponse sync(MemorySyncRequest request) {
        validateSync(request);
        JsonNode data = send(SYNC_PATH, request, syncTimeout, "记忆同步", request.userId());

        String collection = requiredText(data, "collection", "记忆同步");
        String pointId = null;
        if (data.hasNonNull("pointId")) {
            JsonNode node = data.get("pointId");
            if (!node.isTextual() || node.asText().isBlank()) {
                throw contractError("记忆同步", "pointId 必须是非空字符串或 null");
            }
            pointId = node.asText();
        }
        JsonNode skipped = data.get("skipped");
        if (skipped == null || !skipped.isBoolean()) {
            throw contractError("记忆同步", "skipped 必须是布尔值");
        }
        return new MemorySyncResponse(pointId, collection, skipped.asBoolean());
    }

    /**
     * 向量路检索。返回的候选<b>只有 id / score / contentHash</b>；
     * 调用方必须回 MySQL 复核 ACTIVE 与可见性后才能使用。
     */
    public List<MemoryVectorHit> search(MemoryVectorSearchRequest request) {
        validateSearch(request);
        JsonNode data = send(SEARCH_PATH, request, searchTimeout, "记忆向量检索",
                request.userId());

        JsonNode hits = data.get("hits");
        if (hits == null || !hits.isArray()) {
            throw contractError("记忆向量检索", "响应缺少 hits 数组");
        }
        logWarnings(data);

        List<MemoryVectorHit> result = new ArrayList<>(hits.size());
        for (JsonNode hit : hits) {
            JsonNode id = hit.get("memoryId");
            JsonNode score = hit.get("score");
            if (id == null || !id.isIntegralNumber() || id.asLong() <= 0) {
                throw contractError("记忆向量检索", "memoryId 必须是正整数");
            }
            if (score == null || !score.isNumber() || !Double.isFinite(score.asDouble())) {
                throw contractError("记忆向量检索", "score 必须是有限数值");
            }
            JsonNode hash = hit.get("contentHash");
            if (hash != null && !hash.isNull() && !hash.isTextual()) {
                throw contractError("记忆向量检索", "contentHash 必须是字符串或 null");
            }
            result.add(new MemoryVectorHit(
                    id.asLong(),
                    score.asDouble(),
                    hash == null || hash.isNull() ? null : hash.asText()));
        }
        return result;
    }

    // ===================== 校验 =====================

    private void validateSync(MemorySyncRequest request) {
        if (request == null) {
            throw new AgentMemoryException(422, "记忆同步请求不能为空");
        }
        requirePositive(request.memoryId(), "memoryId");
        requirePositive(request.userId(), "userId");
        String operation = request.operation() == null
                ? "" : request.operation().trim().toUpperCase(Locale.ROOT);
        if (!"UPSERT".equals(operation) && !"DELETE".equals(operation)) {
            throw new AgentMemoryException(422, "operation 只支持 UPSERT / DELETE");
        }
        if ("UPSERT".equals(operation)
                && (request.content() == null || request.content().isBlank())) {
            throw new AgentMemoryException(422, "UPSERT 的 content 不能为空");
        }
    }

    private void validateSearch(MemoryVectorSearchRequest request) {
        if (request == null) {
            throw new AgentMemoryException(422, "记忆向量检索请求不能为空");
        }
        requirePositive(request.userId(), "userId");
        if (request.query() == null || request.query().isBlank()) {
            throw new AgentMemoryException(422, "query 不能为空");
        }
        if (request.recallK() != null && request.recallK() <= 0) {
            throw new AgentMemoryException(422, "recallK 必须为正数");
        }
    }

    private static void requirePositive(Long value, String field) {
        if (value == null || value <= 0) {
            throw new AgentMemoryException(422, field + " 必须是正整数");
        }
    }

    // ===================== HTTP =====================

    private JsonNode send(String path, Object body, Duration timeout, String action,
                          Long userId) {
        requireInternalToken(action);
        final String identity;
        // 记忆归属用户由调用方（Outbox 事件）提供；缺密钥时 fail closed，
        // 避免记忆以“无主”或可伪造身份进入向量库。
        try {
            identity = identitySigner.sign(userId);
        } catch (RuntimeException e) {
            throw new AgentMemoryException(503,
                    "未配置 ails.internal.identity-secret，无法签发内部身份", e);
        }

        byte[] payload = JsonUtil.toJson(body).getBytes(StandardCharsets.UTF_8);
        HttpRequest request;
        try {
            request = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + path))
                    .timeout(timeout)
                    .header("Content-Type", "application/json")
                    .header("Accept", "application/json")
                    .header(TOKEN_HEADER, internalToken)
                    .header(IDENTITY_HEADER, identity)
                    .header(USER_HEADER, String.valueOf(userId))
                    .POST(HttpRequest.BodyPublishers.ofByteArray(payload))
                    .build();
        } catch (RuntimeException e) {
            throw new AgentMemoryException(500, "构造 Agent " + action + "请求失败", e);
        }
        return sendRequest(request, action);
    }

    private JsonNode sendRequest(HttpRequest request, String action) {
        HttpResponse<String> response;
        try {
            response = client.send(request,
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (HttpConnectTimeoutException | ConnectException e) {
            throw new AgentMemoryException(503, "Agent 服务未启动或无法连接", e);
        } catch (HttpTimeoutException e) {
            throw new AgentMemoryException(504, "等待 Agent " + action + "超时", e);
        } catch (IOException e) {
            throw new AgentMemoryException(503, "连接 Agent 失败", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new AgentMemoryException(500, "Agent " + action + "请求被中断", e);
        }

        if (response.statusCode() >= 400) {
            throw new AgentMemoryException(response.statusCode(),
                    "Agent " + action + "失败（HTTP " + response.statusCode() + "）："
                            + abbreviate(response.body(), 300));
        }
        try {
            JsonNode root = MAPPER.readTree(response.body());
            int code = root.path("code").asInt(-1);
            if (code != 0) {
                int status = code >= 400 && code < 600 ? code : 502;
                throw new AgentMemoryException(status,
                        "Agent " + action + "失败："
                                + root.path("message").asText("code=" + code));
            }
            JsonNode data = root.path("data");
            if (!data.isObject()) {
                throw contractError(action, "响应缺少 data");
            }
            return data;
        } catch (AgentMemoryException e) {
            throw e;
        } catch (Exception e) {
            // 契约漂移不是暂时性故障：重试不会把字段名改回来，直接按永久失败处理。
            throw new AgentMemoryException(502,
                    "Agent " + action + "响应不是合法 JSON", e, true);
        }
    }

    private void requireInternalToken(String action) {
        if (internalToken.isEmpty()) {
            throw new AgentMemoryException(503,
                    "未配置 ails.internal.token，无法调用 Agent " + action + "接口");
        }
    }

    private static String requiredText(JsonNode data, String field, String action) {
        JsonNode node = data.get(field);
        if (node == null || !node.isTextual() || node.asText().isBlank()) {
            throw contractError(action, field + " 必须是非空字符串");
        }
        return node.asText();
    }

    private static AgentMemoryException contractError(String action, String detail) {
        return new AgentMemoryException(502,
                "Agent " + action + "响应不符合契约：" + detail, null, true);
    }

    private static void logWarnings(JsonNode data) {
        JsonNode warnings = data.get("warnings");
        if (warnings == null || !warnings.isArray() || warnings.isEmpty()) {
            return;
        }
        List<String> messages = new ArrayList<>(warnings.size());
        warnings.forEach(node -> messages.add(node.asText()));
        log.warn("Agent 记忆向量检索返回 warning：{}", messages);
    }

    private static long positive(long value, long fallback) {
        return value > 0 ? value : fallback;
    }

    private static String trimTrailingSlash(String url) {
        return url != null && url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
    }

    private static String abbreviate(String text, int max) {
        if (text == null) {
            return "";
        }
        return text.length() <= max ? text : text.substring(0, max) + "…";
    }
}
