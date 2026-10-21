package com.ails.backend.agent;

import com.ails.backend.dto.agent.AnalyzeAgentRequest;
import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.dto.agent.ChatHistoryMessage;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.VerificationAction;
import com.ails.backend.enums.VerificationTrigger;
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
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * {@link HttpPythonAgentAnalysisGateway} 的契约测试。
 * <p>
 * 与 Chat 网关一样，用 JDK 自带的 {@link HttpServer} 起一个假 Agent，不起 Spring 上下文。
 * 重点锁住三处：
 * <ol>
 *   <li>{@code {"code":0,"data":{...}}} 的解析与嵌套字段映射（Agent 与 Java 的字段名靠 alias 对齐）；</li>
 *   <li>失败映射 —— 调用方据此决定是否退回 rules-v1，状态码被写成 500 会让排查方向彻底跑偏；</li>
 *   <li>请求体就是 {@link AnalyzeAgentRequest} 本身（camelCase、含 messageId / assistantMessage）。</li>
 * </ol>
 */
class HttpPythonAgentAnalysisGatewayTest {

    private static final String TRACE_ID = "0123456789abcdef0123456789abcdef";

    private final List<HttpServer> servers = new ArrayList<>();
    private final AtomicReference<String> lastRequestBody = new AtomicReference<>("");
    private final AtomicReference<String> lastRequestPath = new AtomicReference<>("");

    @AfterEach
    void stopServers() {
        servers.forEach(server -> server.stop(0));
        servers.clear();
    }

    @Test
    @DisplayName("成功响应：证据 / 提案 / 记忆 / 验证决策全部按契约解析")
    void parsesSuccessfulResult() throws IOException {
        HttpServer server = startServer(200, """
                {"code":0,"data":{
                  "evidenceCandidates":[{
                    "conceptId":14,"evidenceType":"USER_EXPLANATION",
                    "observation":"TLB 是页表的缓存","correctness":"PARTIAL",
                    "reasoningQuality":0.7,"specificity":0.6,"aiConfidence":0.8,
                    "needFollowUp":false,"userExplanation":"TLB 是页表的缓存"
                  }],
                  "understandingProposal":{
                    "conceptId":14,"knownParts":["TLB 是页表的缓存"],
                    "uncertainParts":["未提到 TLB Miss"],"misconceptions":["把 TLB Miss 当成 Page Fault"]
                  },
                  "memoryCandidates":[{
                    "scopeType":"GLOBAL","memoryType":"PREFERENCE","memoryKey":"pref/example",
                    "content":"喜欢先看例子再看定义","importance":0.8,"action":"ADD"
                  }],
                  "verificationDecision":{
                    "action":"ASK_NOW","trigger":"NEW_CONCEPT","conceptId":14,
                    "reason":"从未验证过","questionType":"SHORT_ANSWER",
                    "questionText":"请解释 TLB Miss 之后发生什么","correctAnswer":"查页表"
                  },
                  "analyzerVersion":"agent-v1",
                  "detectedConcepts":["高速缓存"],
                  "futureField":"以后再加的字段不应导致失败"
                }}
                """);

        AnalyzeAgentResult result = gateway(server).analyze(sampleRequest());

        assertThat(result.evidenceCandidates()).hasSize(1);
        AnalyzeAgentResult.EvidenceCandidate evidence = result.evidenceCandidates().get(0);
        assertThat(evidence.conceptId()).isEqualTo(14L);
        assertThat(evidence.evidenceType()).isEqualTo(EvidenceType.USER_EXPLANATION);
        assertThat(evidence.correctness()).isEqualTo(EvidenceCorrectness.PARTIAL);
        assertThat(evidence.reasoningQuality()).isEqualTo(0.7f);

        assertThat(result.understandingProposal().knownParts())
                .containsExactly("TLB 是页表的缓存");
        assertThat(result.understandingProposal().misconceptions())
                .containsExactly("把 TLB Miss 当成 Page Fault");

        AnalyzeAgentResult.MemoryCandidate memory = result.memoryCandidates().get(0);
        assertThat(memory.scopeType()).isEqualTo(MemoryScopeType.GLOBAL);
        assertThat(memory.memoryType()).isEqualTo(MemoryType.PREFERENCE);
        assertThat(memory.action()).isEqualTo(MemoryReconcileAction.ADD);

        assertThat(result.verificationDecision().action()).isEqualTo(VerificationAction.ASK_NOW);
        assertThat(result.verificationDecision().trigger()).isEqualTo(VerificationTrigger.NEW_CONCEPT);
        assertThat(result.verificationDecision().questionType()).isEqualTo(QuestionType.SHORT_ANSWER);
        assertThat(result.detectedConcepts()).containsExactly("高速缓存");
        assertThat(result.analyzerVersion()).isEqualTo("agent-v1");
    }

    @Test
    @DisplayName("返回业务错误 code != 0：按 502 抛出，不进解析")
    void mapsBusinessErrorToBadGateway() throws IOException {
        HttpServer server = startServer(200, """
                {"code":502,"message":"分析结果不符合契约"}
                """);

        assertThatThrownBy(() -> gateway(server).analyze(sampleRequest()))
                .isInstanceOf(AgentAnalysisException.class)
                .hasMessageContaining("分析结果不符合契约")
                .satisfies(e -> assertThat(((AgentAnalysisException) e).statusCode()).isEqualTo(502));
    }

    @Test
    @DisplayName("缺 data：按 502 抛出，不允许把半份结果写进库")
    void missingDataIsBadGateway() throws IOException {
        HttpServer server = startServer(200, "{\"code\":0}");

        assertThatThrownBy(() -> gateway(server).analyze(sampleRequest()))
                .isInstanceOf(AgentAnalysisException.class)
                .satisfies(e -> assertThat(((AgentAnalysisException) e).statusCode()).isEqualTo(502));
    }

    @Test
    @DisplayName("响应不是 JSON：按 502 抛出")
    void malformedJsonIsBadGateway() throws IOException {
        HttpServer server = startServer(200, "not-json");

        assertThatThrownBy(() -> gateway(server).analyze(sampleRequest()))
                .isInstanceOf(AgentAnalysisException.class)
                .satisfies(e -> assertThat(((AgentAnalysisException) e).statusCode()).isEqualTo(502));
    }

    @Test
    @DisplayName("上游 HTTP 500：保留上游状态码")
    void mapsUpstreamHttpStatus() throws IOException {
        HttpServer server = startServer(500, "{\"detail\":\"boom\"}");

        assertThatThrownBy(() -> gateway(server).analyze(sampleRequest()))
                .isInstanceOf(AgentAnalysisException.class)
                .satisfies(e -> assertThat(((AgentAnalysisException) e).statusCode()).isEqualTo(500));
    }

    @Test
    @DisplayName("Agent 没起：按 503 抛出")
    void connectionRefusedIsUnavailable() throws IOException {
        int deadPort;
        try (ServerSocket socket = new ServerSocket(0)) {
            deadPort = socket.getLocalPort();
        }
        HttpPythonAgentAnalysisGateway gateway = new HttpPythonAgentAnalysisGateway(
                "http://127.0.0.1:" + deadPort, 1000, 5000);

        assertThatThrownBy(() -> gateway.analyze(sampleRequest()))
                .isInstanceOf(AgentAnalysisException.class)
                .satisfies(e -> assertThat(((AgentAnalysisException) e).statusCode()).isEqualTo(503));
    }

    @Test
    @DisplayName("请求体是 AnalyzeAgentRequest 本身：路径、messageId、assistantMessage 都对齐")
    void sendsRequestAsIs() throws IOException {
        HttpServer server = startServer(200, "{\"code\":0,\"data\":{}}");

        gateway(server).analyze(sampleRequest());

        assertThat(lastRequestPath.get()).isEqualTo("/agent/analyze");
        assertThat(lastRequestBody.get()).contains("\"runId\":2001");
        assertThat(lastRequestBody.get()).contains("\"traceId\":\"" + TRACE_ID + "\"");
        assertThat(lastRequestBody.get()).contains("\"messageId\":1001");
        assertThat(lastRequestBody.get()).contains("\"userMessage\":\"TLB Miss 之后呢\"");
        assertThat(lastRequestBody.get()).contains("\"assistantMessage\":\"TLB Miss 会去查页表\"");
        assertThat(lastRequestBody.get()).contains("\"learningSpaceId\":3");
        assertThat(lastRequestBody.get()).contains("\"authToken\":\"jwt-token\"");
        assertThat(lastRequestBody.get()).contains("\"role\":\"USER\"");
        assertThat(lastRequestBody.get()).contains("\"goal\":\"理解地址转换\"");
    }

    @Test
    @DisplayName("data 为空对象时全部字段取默认值（空列表 + SKIP），不会 NPE")
    void emptyDataUsesDefaults() throws IOException {
        HttpServer server = startServer(200, "{\"code\":0,\"data\":{}}");

        AnalyzeAgentResult result = gateway(server).analyze(sampleRequest());

        assertThat(result.evidenceCandidates()).isEmpty();
        assertThat(result.memoryCandidates()).isEmpty();
        assertThat(result.detectedConcepts()).isEmpty();
        assertThat(result.verificationDecision().action()).isEqualTo(VerificationAction.SKIP);
        assertThat(result.analyzerVersion()).isEqualTo("agent-v1");
    }

    // ---------- 脚手架 ----------

    private HttpPythonAgentAnalysisGateway gateway(HttpServer server) {
        return new HttpPythonAgentAnalysisGateway(
                "http://127.0.0.1:" + server.getAddress().getPort(), 2000, 10000);
    }

    private HttpServer startServer(int status, String body) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/agent/analyze", exchange -> {
            lastRequestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            lastRequestPath.set(exchange.getRequestURI().getPath());
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
            exchange.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        });
        server.start();
        servers.add(server);
        return server;
    }

    private AnalyzeAgentRequest sampleRequest() {
        return new AnalyzeAgentRequest(
                2001L,
                TRACE_ID,
                1L,
                2L,
                3L,
                1001L,
                "TLB Miss 之后呢",
                "TLB Miss 会去查页表",
                List.of(new ChatHistoryMessage(MessageRole.USER, "上一轮的问题")),
                new ChatAgentRequest.SessionContext(20L, "虚拟内存", "理解地址转换", "卡在 TLB Miss"),
                "jwt-token");
    }
}
