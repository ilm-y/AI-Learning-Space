package com.ails.backend.service;

import com.ails.backend.agent.AgentGateway;
import com.ails.backend.agent.AgentStreamHandler;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.utils.JwtUtil;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.mock.mockito.SpyBean;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
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
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

/**
 * SSE 主动断开契约 —— 真实 HTTP 服务 + 真实 MySQL + 真实 ChatService/MessageService/规则分析。
 *
 * <p>默认跳过。启用：</p>
 * <pre>
 * AILS_CHAT_SSE_IT=true AILS_IT_MYSQL_PASSWORD=... \
 * ./mvnw -Dtest=ChatSseIntegrationTest test
 * </pre>
 *
 * <p><b>这不是 Controller 层 HTTP 状态测试。</b>测试启动真实 Tomcat（随机端口），用
 * {@link HttpClient} 读取 {@code text/event-stream}，在收到指定事件后主动关闭响应体来模拟
 * 客户端断开；消息、agent_run、Evidence 全部由真实服务写入独立 MySQL 库
 * {@code ails_chat_sse_it}，断言直接读库。</p>
 *
 * <p>Agent 侧被替换为 {@link ControllableAgentGateway}：本阶段验证的是后端 SSE 状态机，
 * 真实 Agent 的 MQ/HTTP 链路已在 Phase 2.1 的 MQ E2E 中覆盖。分析边界用
 * {@code @SpyBean} 精确卡住，确认“客户端断开后分析仍会继续”，而不是靠 sleep 猜时序。</p>
 */
@EnabledIfEnvironmentVariable(named = "AILS_CHAT_SSE_IT", matches = "true")
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
@Timeout(90)
class ChatSseIntegrationTest {

    private static final String QUESTION = "TLB 缓存的是什么？";
    private static final String ANSWER_PART_1 = "TLB 缓存的是页表项。";
    private static final String ANSWER_PART_2 = "它让地址翻译不必每次访问内存。";

    @LocalServerPort
    private int port;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private JwtUtil jwtUtil;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private ControllableAgentGateway agentGateway;

    @SpyBean
    private AnalysisDispatchService analysisDispatchService;

    @SpyBean
    private MessageService messageService;

    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    private Long userId;
    private Long otherUserId;
    private Long spaceId;
    private Long conceptId;
    private Long conversationId;
    private String token;

    @DynamicPropertySource
    static void testProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", () -> env("AILS_CHAT_SSE_IT_MYSQL_URL",
                "jdbc:mysql://127.0.0.1:3306/ails_chat_sse_it"
                        + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
                        + "&useSSL=false&allowPublicKeyRetrieval=true&createDatabaseIfNotExist=true"));
        registry.add("spring.datasource.username", () -> env("AILS_IT_MYSQL_USER", "root"));
        registry.add("spring.datasource.password", () -> env("AILS_IT_MYSQL_PASSWORD", ""));
    }

    @BeforeEach
    void setUp() {
        clearDatabase();
        String suffix = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        userId = insertUser("chat_sse_it_" + suffix);
        otherUserId = insertUser("chat_sse_it_other_" + suffix);
        spaceId = insertSpace(userId);
        insertSpace(otherUserId);
        conceptId = insertConcept(spaceId);
        conversationId = insertConversation(userId);
        token = jwtUtil.generateToken(userId, "chat_sse_it_" + suffix);
        agentGateway.reset();
    }

    // ===================== 1. 正常路径与顺序 =====================

    @Test
    @DisplayName("正常流：start→delta→end，end 到达时助手消息已落库，之后才执行分析")
    void normalStreamPersistsBeforeEndThenAnalyzes() throws Exception {
        CountDownLatch analysisEntered = new CountDownLatch(1);
        CountDownLatch releaseAnalysis = new CountDownLatch(1);
        blockAnalysis(analysisEntered, releaseAnalysis);

        List<SseEvent> events = new ArrayList<>();
        try (SseStream stream = openStream(conversationId, token, QUESTION)) {
            assertThat(stream.contentType()).contains("text/event-stream");
            SseEvent event;
            while ((event = stream.readEvent()) != null) {
                events.add(event);
                if ("message.end".equals(event.name()) || "message.error".equals(event.name())) {
                    break;
                }
            }

            assertThat(events.get(0).name()).isEqualTo("message.start");
            assertThat(events.subList(1, events.size() - 1))
                    .isNotEmpty()
                    .allSatisfy(item -> {
                        assertThat(item.name()).isEqualTo("message.delta");
                        assertThat(item.data()).contains("delta");
                    });
            assertThat(events.get(events.size() - 1).name()).isEqualTo("message.end");
            JsonNode end = objectMapper.readTree(events.get(events.size() - 1).data());
            assertThat(end.get("messageId").asLong()).isPositive();

            // message.end 已经到达，说明助手消息在服务端发送该事件之前就已提交。
            assertThat(count("message")).isEqualTo(2);
            assertThat(countMessages("ASSISTANT")).isEqualTo(1);
            Message assistant = jdbc.queryForObject(
                    "SELECT id, content FROM message WHERE role = 'ASSISTANT'",
                    (rs, rowNum) -> {
                        Message message = new Message();
                        message.setId(rs.getLong("id"));
                        message.setContent(rs.getString("content"));
                        return message;
                    });
            assertThat(assistant.getId()).isEqualTo(end.get("messageId").asLong());
            assertThat(assistant.getContent()).isEqualTo(ANSWER_PART_1 + ANSWER_PART_2);
            assertThat(runStatus()).isEqualTo("COMPLETED");

            // 分析被卡在入口：end 之后、证据写入之前，库里还没有 Evidence。
            assertThat(analysisEntered.await(10, TimeUnit.SECONDS)).isTrue();
            assertThat(count("learning_evidence")).isZero();

            releaseAnalysis.countDown();
            await("规则分析写入 Evidence", () -> count("learning_evidence") == 1);
            assertThat(stream.readEvent()).isNull(); // 服务端正常关流
        } finally {
            releaseAnalysis.countDown();
        }
    }

    // ===================== 2. 客户端在助手消息落库后断开 =====================

    @Test
    @DisplayName("助手消息落库并收到 end 后客户端断开：分析仍继续，重连不产生重复消息/Revision")
    void disconnectAfterAssistantPersistedStillAnalyzesAndReconnectIsNotDuplicated() throws Exception {
        CountDownLatch analysisEntered = new CountDownLatch(1);
        CountDownLatch releaseAnalysis = new CountDownLatch(1);
        blockAnalysis(analysisEntered, releaseAnalysis);

        SseStream stream = openStream(conversationId, token, QUESTION);
        try {
            SseEvent end = readUntil(stream, "message.end");
            assertThat(end).isNotNull();
            // 关闭响应体 = 客户端主动断开（Tomcat 侧会感知连接终止）。
            stream.close();

            assertThat(analysisEntered.await(10, TimeUnit.SECONDS)).isTrue();
            assertThat(countMessages("ASSISTANT")).isEqualTo(1);
            assertThat(runStatus()).isEqualTo("COMPLETED");

            releaseAnalysis.countDown();
            await("客户端断开后分析仍写入 Evidence", () -> count("learning_evidence") == 1);
        } finally {
            stream.close();
            releaseAnalysis.countDown();
        }

        List<JsonNode> first = listMessages();
        List<JsonNode> second = listMessages();
        assertThat(first).hasSize(2);
        assertThat(first).extracting(node -> node.get("id").asLong())
                .containsExactlyElementsOf(second.stream().map(node -> node.get("id").asLong()).toList());
        assertThat(count("message")).isEqualTo(2);
        assertThat(count("understanding_revision")).isEqualTo(1);
    }

    // ===================== 3. 助手消息未落库 =====================

    @Test
    @DisplayName("助手消息持久化失败：发送 message.error，run=FAILED，禁止启动分析")
    void assistantPersistenceFailureSendsErrorAndSkipsAnalysis() throws Exception {
        doThrow(new RuntimeException("database unavailable"))
                .when(messageService).appendAssistantMessage(eq(conversationId), anyString());

        List<SseEvent> events = new ArrayList<>();
        try (SseStream stream = openStream(conversationId, token, QUESTION)) {
            SseEvent event;
            while ((event = stream.readEvent()) != null) {
                events.add(event);
                if ("message.end".equals(event.name()) || "message.error".equals(event.name())) {
                    break;
                }
            }
            assertThat(events.get(0).name()).isEqualTo("message.start");
            assertThat(events.subList(1, events.size() - 1))
                    .isNotEmpty()
                    .allSatisfy(item -> assertThat(item.name()).isEqualTo("message.delta"));
            assertThat(events.get(events.size() - 1).name()).isEqualTo("message.error");
            JsonNode error = objectMapper.readTree(events.get(events.size() - 1).data());
            assertThat(error.get("code").asInt()).isEqualTo(500);
            assertThat(error.get("message").asText()).isEqualTo("回复保存失败");
            assertThat(stream.readEvent()).isNull();
        }

        assertThat(countMessages("ASSISTANT")).isZero();
        assertThat(countMessages("USER")).isEqualTo(1);
        assertThat(runStatus()).isEqualTo("FAILED");
        assertThat(count("learning_evidence")).isZero();
        verify(analysisDispatchService, never()).dispatch(
                any(Conversation.class), any(Message.class), anyString(), anyLong(),
                anyString(), any(LearningAnalysisService.TurnOpening.class));
    }

    // ===================== 4. LLM 空流 / 模型错误终态 =====================

    @Test
    @DisplayName("LLM 空流：message.error(code=502)，run=FAILED，无助手消息、无分析")
    void emptyStreamIsTerminalError() throws Exception {
        agentGateway.emptyStream();

        List<SseEvent> events = readTerminalEvents();

        assertThat(events).extracting(SseEvent::name)
                .containsExactly("message.start", "message.error");
        JsonNode error = objectMapper.readTree(events.get(1).data());
        assertThat(error.get("code").asInt()).isEqualTo(502);
        assertThat(error.get("message").asText()).contains("empty_stream");
        assertThat(countMessages("ASSISTANT")).isZero();
        await("空流终态写入", () -> "FAILED".equals(runStatus()));
        assertThat(count("learning_evidence")).isZero();
    }

    @Test
    @DisplayName("模型错误：message.error(code=502)，run=FAILED，无助手消息、无分析")
    void modelErrorIsTerminalError() throws Exception {
        agentGateway.modelError();

        List<SseEvent> events = readTerminalEvents();

        assertThat(events).extracting(SseEvent::name)
                .containsExactly("message.start", "message.error");
        JsonNode error = objectMapper.readTree(events.get(1).data());
        assertThat(error.get("code").asInt()).isEqualTo(502);
        assertThat(error.get("message").asText()).contains("quota exceeded");
        assertThat(countMessages("ASSISTANT")).isZero();
        await("模型错误终态写入", () -> "FAILED".equals(runStatus()));
        assertThat(count("learning_evidence")).isZero();
    }

    // ===================== 5. 重复请求 / 越权 =====================

    @Test
    @DisplayName("未实现：无幂等键，重复 POST 同一内容会重复落库（作为缺口证据记录，不计为通过）")
    void duplicatePostWithoutIdempotencyKeyCreatesDuplicateMessages() throws Exception {
        // 第一次请求
        try (SseStream stream = openStream(conversationId, token, QUESTION)) {
            assertThat(readUntil(stream, "message.end")).isNotNull();
        }
        // 第二次请求：服务端没有 clientRequestId / 幂等键，无法区分“重试”和“再问一次”。
        try (SseStream stream = openStream(conversationId, token, QUESTION)) {
            assertThat(readUntil(stream, "message.end")).isNotNull();
        }

        assertThat(countMessages("USER")).isEqualTo(2);
        assertThat(countMessages("ASSISTANT")).isEqualTo(2);
        assertThat(countRuns("CHAT")).isEqualTo(2);
        // 相同内容产生的 Evidence 由 dedupe_key 收敛，Revision 不重复；重复的只是消息与 run。
        assertThat(count("learning_evidence")).isEqualTo(1);
        assertThat(count("understanding_revision")).isEqualTo(1);
    }

    @Test
    @DisplayName("向他人对话发消息：404，且不写入任何消息")
    void crossUserConversationIsRejectedBeforePersistence() throws Exception {
        Long otherConversationId = insertConversation(otherUserId);

        HttpResponse<String> response = postJson(otherConversationId, token, QUESTION);

        assertThat(response.statusCode()).as("body=%s", response.body()).isEqualTo(404);
        assertThat(response.body()).containsAnyOf("学习空间不存在", "对话不存在");
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM message WHERE conversation_id = ?", Integer.class,
                otherConversationId)).isZero();
    }

    // ===================== 辅助 =====================

    private void blockAnalysis(CountDownLatch entered, CountDownLatch release) {
        doAnswer(invocation -> {
            entered.countDown();
            if (!release.await(30, TimeUnit.SECONDS)) {
                throw new IllegalStateException("test did not release analysis in time");
            }
            return invocation.callRealMethod();
        }).when(analysisDispatchService).dispatch(
                any(Conversation.class), any(Message.class), anyString(), anyLong(),
                anyString(), any(LearningAnalysisService.TurnOpening.class));
    }

    private List<SseEvent> readTerminalEvents() throws Exception {
        List<SseEvent> events = new ArrayList<>();
        try (SseStream stream = openStream(conversationId, token, QUESTION)) {
            SseEvent event;
            while ((event = stream.readEvent()) != null) {
                events.add(event);
                if ("message.end".equals(event.name()) || "message.error".equals(event.name())) {
                    break;
                }
            }
        }
        return events;
    }

    private SseEvent readUntil(SseStream stream, String terminalName) throws Exception {
        SseEvent event;
        while ((event = stream.readEvent()) != null) {
            if (terminalName.equals(event.name())) {
                return event;
            }
            if ("message.error".equals(event.name())) {
                return event;
            }
        }
        return null;
    }

    private SseStream openStream(Long conversation, String authToken, String content) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/conversations/" + conversation + "/messages"))
                .timeout(Duration.ofSeconds(30))
                .header("Authorization", "Bearer " + authToken)
                .header("Accept", "text/event-stream")
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"content\":\"" + content + "\"}", StandardCharsets.UTF_8))
                .build();
        HttpResponse<InputStream> response = http.send(request, HttpResponse.BodyHandlers.ofInputStream());
        assertThat(response.statusCode()).isEqualTo(200);
        return new SseStream(response.body(),
                response.headers().firstValue("Content-Type").orElse(""));
    }

    private HttpResponse<String> postJson(Long conversation, String authToken, String content) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/conversations/" + conversation + "/messages"))
                .timeout(Duration.ofSeconds(30))
                .header("Authorization", "Bearer " + authToken)
                .header("Accept", "text/event-stream")
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"content\":\"" + content + "\"}", StandardCharsets.UTF_8))
                .build();
        return http.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private List<JsonNode> listMessages() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/conversations/" + conversationId + "/messages"))
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + token)
                .GET()
                .build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertThat(response.statusCode()).isEqualTo(200);
        JsonNode data = objectMapper.readTree(response.body()).get("data");
        List<JsonNode> messages = new ArrayList<>();
        data.forEach(messages::add);
        return messages;
    }

    private String baseUrl() {
        return "http://127.0.0.1:" + port;
    }

    private String runStatus() {
        return jdbc.queryForObject(
                "SELECT status FROM agent_run WHERE run_type = 'CHAT' ORDER BY id DESC LIMIT 1",
                String.class);
    }

    private int countRuns(String runType) {
        Integer value = jdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_run WHERE run_type = ?", Integer.class, runType);
        return value == null ? 0 : value;
    }

    private int countMessages(String role) {
        Integer value = jdbc.queryForObject(
                "SELECT COUNT(*) FROM message WHERE role = ?", Integer.class, role);
        return value == null ? 0 : value;
    }

    private int count(String table) {
        Integer value = jdbc.queryForObject("SELECT COUNT(*) FROM `" + table + "`", Integer.class);
        return value == null ? 0 : value;
    }

    private static void await(String description, BooleanSupplier condition) throws InterruptedException {
        long deadline = System.currentTimeMillis() + 15_000;
        while (System.currentTimeMillis() < deadline) {
            if (condition.getAsBoolean()) {
                return;
            }
            Thread.sleep(50);
        }
        throw new AssertionError("等待超时：" + description);
    }

    private Long insertUser(String username) {
        jdbc.update("INSERT INTO user (username, password_hash) VALUES (?, ?)", username, "x");
        return jdbc.queryForObject("SELECT id FROM user WHERE username = ?", Long.class, username);
    }

    private Long insertSpace(Long ownerId) {
        jdbc.update("INSERT INTO learning_space (user_id, name, visibility) VALUES (?, ?, 'PRIVATE')",
                ownerId, "chat-sse-it-space-" + UUID.randomUUID().toString().substring(0, 8));
        return jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    private Long insertConcept(Long learningSpaceId) {
        jdbc.update("INSERT INTO concept (learning_space_id, canonical_name, display_name) VALUES (?, ?, ?)",
                learningSpaceId, "TLB", "TLB");
        return jdbc.queryForObject(
                "SELECT id FROM concept WHERE learning_space_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, learningSpaceId);
    }

    private Long insertConversation(Long ownerId) {
        Long ownerSpace = jdbc.queryForObject(
                "SELECT id FROM learning_space WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
        jdbc.update("INSERT INTO conversation (user_id, learning_space_id, title, status) "
                        + "VALUES (?, ?, ?, 'ACTIVE')",
                ownerId, ownerSpace, "chat-sse-it-conversation");
        return jdbc.queryForObject(
                "SELECT id FROM conversation WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, ownerId);
    }

    private void clearDatabase() {
        List<String> tables = List.of(
                "memory_source", "memory", "verification_answer", "verification_question",
                "evidence_source", "understanding_revision", "learning_evidence",
                "learning_understanding", "learning_state", "agent_run", "message",
                "highlight", "conversation", "learning_session", "concept_relation", "concept",
                "test_answer", "test_task", "learning_plan", "import_task", "document_chunk",
                "document", "learning_space", "user");
        jdbc.execute("SET FOREIGN_KEY_CHECKS=0");
        try {
            for (String table : tables) {
                try {
                    jdbc.update("DELETE FROM `" + table + "`");
                } catch (RuntimeException ignored) {
                    // 独立测试库的 schema 可能还没有该表；其余表照常清理。
                }
            }
        } finally {
            jdbc.execute("SET FOREIGN_KEY_CHECKS=1");
        }
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }

    /** 可控制的 Agent 网关：只验证后端 SSE 状态机，不访问网络或真实模型。 */
    static final class ControllableAgentGateway implements AgentGateway {

        private volatile Mode mode = Mode.NORMAL;

        void reset() {
            mode = Mode.NORMAL;
        }

        void emptyStream() {
            mode = Mode.EMPTY_STREAM;
        }

        void modelError() {
            mode = Mode.MODEL_ERROR;
        }

        @Override
        public void streamReply(ChatAgentRequest request, AgentStreamHandler handler) {
            switch (mode) {
                case EMPTY_STREAM -> handler.onError(502,
                        "模型返回空流（reason=empty_stream, status=200, attempts=2）");
                case MODEL_ERROR -> handler.onError(502, "模型服务错误（quota exceeded）");
                case NORMAL -> {
                    handler.onDelta(ANSWER_PART_1);
                    handler.onDelta(ANSWER_PART_2);
                    handler.onComplete();
                }
            }
        }

        private enum Mode {
            NORMAL, EMPTY_STREAM, MODEL_ERROR
        }
    }

    @TestConfiguration
    static class GatewayOverride {

        @Bean
        @Primary
        ControllableAgentGateway controllableAgentGateway() {
            return new ControllableAgentGateway();
        }
    }

    /** 真实 HTTP 响应体的 SSE 读取器；close() 模拟客户端主动断开。 */
    private static final class SseStream implements AutoCloseable {

        private final InputStream body;
        private final BufferedReader reader;
        private final String contentType;
        private boolean closed;

        private SseStream(InputStream body, String contentType) {
            this.body = body;
            this.reader = new BufferedReader(new InputStreamReader(body, StandardCharsets.UTF_8));
            this.contentType = contentType;
        }

        String contentType() {
            return contentType;
        }

        SseEvent readEvent() throws Exception {
            String eventName = null;
            StringBuilder data = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.isEmpty()) {
                    if (eventName != null || data.length() > 0) {
                        return new SseEvent(eventName == null ? "message" : eventName, data.toString());
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

        @Override
        public void close() {
            if (closed) {
                return;
            }
            closed = true;
            try {
                reader.close();
            } catch (Exception ignored) {
                // 断开模拟不需要关心关闭异常
            }
            try {
                body.close();
            } catch (Exception ignored) {
                // 同上
            }
        }
    }

    private record SseEvent(String name, String data) {
    }
}
