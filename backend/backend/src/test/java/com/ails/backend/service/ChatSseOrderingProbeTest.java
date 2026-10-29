package com.ails.backend.service;

import com.ails.backend.utils.JwtUtil;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * Phase 8 诊断探针（默认跳过，不做通过/失败判定）。
 *
 * <p>目的只有一个：量一量 {@code message.end} 到达客户端的那一刻，同一轮
 * {@code agent_run} 是否已经落到终态。{@link ChatService} 的顺序是
 * 「存助手消息 → 推 message.end → finishRun」，所以两者之间存在一个真实窗口；
 * 本探针把观察到 RUNNING 的次数打印出来，给「测试改成 await 是否掩盖了竞争」这个问题
 * 一个可复现的实测答案。</p>
 *
 * <p>它不是契约测试：无论观察到 RUNNING 还是 COMPLETED 都算通过，
 * 只断言事件确实到达、最终状态确实收敛。启用：</p>
 * <pre>
 * AILS_CHAT_SSE_IT=true AILS_P8_ORDER_PROBE=true AILS_IT_MYSQL_PASSWORD=... \
 * ./mvnw -Dtest=ChatSseOrderingProbeTest test
 * </pre>
 */
@EnabledIfEnvironmentVariable(named = "AILS_CHAT_SSE_IT", matches = "true")
@EnabledIfEnvironmentVariable(named = "AILS_P8_ORDER_PROBE", matches = "true")
@SpringBootTest(classes = com.ails.backend.BackendApplication.class,
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "spring.sql.init.mode=always",
        "spring.sql.init.schema-locations=classpath:schema.sql",
        "spring.rabbitmq.listener.simple.auto-startup=false",
        "ails.agent.gateway=stub",
        "ails.analysis.mode=inline",
        "ails.internal.token=chat-sse-it-internal-token",
        "ails.internal.identity-secret=chat-sse-it-identity-secret",
        "ails.internal.delegation-secret=chat-sse-it-delegation-secret"
})
@Import(ChatSseIntegrationTest.GatewayOverride.class)
@Timeout(120)
class ChatSseOrderingProbeTest {

    /** 迭代次数：窗口只有一次 DB 更新的宽度，需要足够样本才能观察到。 */
    private static final int ROUNDS = 20;

    @LocalServerPort
    private int port;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private JwtUtil jwtUtil;

    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_CHAT_SSE_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_chat_sse_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
    }

    @Test
    @DisplayName("探针：message.end 到达时 agent_run 的实际状态分布")
    void probeRunStatusAtMessageEnd() throws Exception {
        Map<String, Integer> observed = new LinkedHashMap<>();

        for (int round = 1; round <= ROUNDS; round++) {
            Long userId = insertUser("p8_probe_" + UUID.randomUUID().toString().substring(0, 8));
            Long spaceId = insertSpace(userId);
            Long conversationId = insertConversation(userId, spaceId);
            String token = jwtUtil.generateToken(userId, "p8_probe");

            String status;
            try (SseStream stream = openStream(conversationId, token)) {
                String terminal = readUntilTerminal(stream);
                if (!"message.end".equals(terminal)) {
                    throw new AssertionError("第 " + round + " 轮未收到 message.end，而是 " + terminal);
                }
                // 收到 end 的同一瞬间查库：这就是客户端此时能观察到的状态。
                status = jdbc.queryForObject(
                        "SELECT status FROM agent_run WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                        String.class, conversationId);
            }
            observed.merge(String.valueOf(status), 1, Integer::sum);
            awaitTerminal(conversationId);
        }

        System.out.println("P8_ORDER_PROBE rounds=" + ROUNDS + " observed_at_message_end=" + observed);
    }

    private String readUntilTerminal(SseStream stream) throws Exception {
        String eventName = null;
        StringBuilder data = new StringBuilder();
        String line;
        while ((line = stream.readLine()) != null) {
            if (line.isEmpty()) {
                if (eventName != null) {
                    if ("message.end".equals(eventName) || "message.error".equals(eventName)) {
                        return eventName;
                    }
                    eventName = null;
                    data.setLength(0);
                }
                continue;
            }
            if (line.startsWith("event:")) {
                eventName = line.substring("event:".length()).trim();
            } else if (line.startsWith("data:")) {
                if (data.length() > 0) {
                    data.append('\n');
                }
                data.append(line.substring("data:".length()).trim());
            }
        }
        return null;
    }

    private void awaitTerminal(Long conversationId) throws InterruptedException {
        long deadline = System.currentTimeMillis() + 15_000;
        while (System.currentTimeMillis() < deadline) {
            String status = jdbc.queryForObject(
                    "SELECT status FROM agent_run WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                    String.class, conversationId);
            if ("COMPLETED".equals(status) || "TRUNCATED".equals(status) || "FAILED".equals(status)) {
                return;
            }
            Thread.sleep(20);
        }
        throw new AssertionError("agent_run 未在 15s 内收敛, conversationId=" + conversationId);
    }

    private SseStream openStream(Long conversationId, String token) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/conversations/" + conversationId + "/messages"))
                .timeout(Duration.ofSeconds(30))
                .header("Authorization", "Bearer " + token)
                .header("Accept", "text/event-stream")
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"content\":\"TLB 缓存的是什么？\"}", StandardCharsets.UTF_8))
                .build();
        HttpResponse<InputStream> response = http.send(request, HttpResponse.BodyHandlers.ofInputStream());
        if (response.statusCode() != 200) {
            throw new AssertionError("SSE 响应状态 " + response.statusCode());
        }
        return new SseStream(response.body());
    }

    private Long insertUser(String username) {
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", username, "x");
        return jdbc.queryForObject("SELECT id FROM user WHERE username = ?", Long.class, username);
    }

    private Long insertSpace(Long ownerId) {
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                ownerId, "p8-probe-space-" + UUID.randomUUID().toString().substring(0, 8));
        return jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    private Long insertConversation(Long ownerId, Long spaceId) {
        jdbc.update("INSERT INTO conversation (user_id, learning_space_id, title, status) "
                + "VALUES (?, ?, ?, 'ACTIVE')", ownerId, spaceId, "p8-probe-conversation");
        return jdbc.queryForObject(
                "SELECT id FROM conversation WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }

    /** 只读到终局事件，不解析 data（本探针不关心内容）。 */
    private static final class SseStream implements AutoCloseable {

        private final BufferedReader reader;

        private SseStream(InputStream body) {
            this.reader = new BufferedReader(new InputStreamReader(body, StandardCharsets.UTF_8));
        }

        private String readLine() throws Exception {
            return reader.readLine();
        }

        @Override
        public void close() throws Exception {
            reader.close();
        }
    }
}
