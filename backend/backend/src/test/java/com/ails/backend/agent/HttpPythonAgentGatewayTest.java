package com.ails.backend.agent;

import com.ails.backend.dto.agent.AgentCitation;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.dto.agent.ChatHistoryMessage;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.security.InternalIdentitySigner;
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

/**
 * {@link HttpPythonAgentGateway} 的事件翻译与失败映射测试。
 * <p>
 * 用一个 {@code com.sun.net.httpserver.HttpServer}（JDK 自带，零依赖）在本机起一个假 Agent，
 * 吐固定的 SSE 字节流，因此不需要 Python 进程、不需要 Spring 上下文，也不需要 Mockito / WireMock
 * ——仓库里现有的测试也都是「纯 JUnit 5 + AssertJ」这一种风格。
 *
 * <p>重点覆盖三处<b>最容易写错</b>的地方：
 * <ol>
 *   <li>{@code error} 事件里的 int code 是否被原样透传（翻译层自己编常量就把错误分类丢了）；</li>
 *   <li>终局回调是否<b>恰好一次</b>（「读循环结束」与「收到 done」是两条独立路径，容易双发）；</li>
 *   <li>「流干净结束但没等到 done」是否被当成失败（当成成功就会把半截话落库）。</li>
 * </ol>
 */
class HttpPythonAgentGatewayTest {

    /** {@link #sampleRequest()} 用的固定 trace id，供「原样透传」的断言引用。 */
    private static final String TRACE_ID = "0123456789abcdef0123456789abcdef";

    /** 仅测试用占位凭据；断言的是「有没有随请求发出」，不是秘密本身。 */
    private static final String TEST_TOKEN = "test-internal-token";
    private static final String TEST_IDENTITY_SECRET = "test-identity-secret";

    /** 本测试里所有服务端都在 127.0.0.1 的临时端口上，跑完逐个关掉。 */
    private final List<HttpServer> servers = new ArrayList<>();

    /** 假 Agent 最近收到的一次请求体，供断言「发出去的 JSON 长什么样」。 */
    private final AtomicReference<String> lastRequestBody = new AtomicReference<>("");

    /** 最近一次请求的 Connection 头，用于钉住「不发 h2c 升级」。 */
    private final AtomicReference<String> lastConnectionHeader = new AtomicReference<>("");

    /** 内网身份头：Agent 侧没有它们会 401。 */
    private final AtomicReference<String> lastTokenHeader = new AtomicReference<>("");
    private final AtomicReference<String> lastUserHeader = new AtomicReference<>("");
    private final AtomicReference<String> lastIdentityHeader = new AtomicReference<>("");
    private final AtomicInteger requestCount = new AtomicInteger();

    @AfterEach
    void stopServers() {
        servers.forEach(server -> server.stop(0));
        servers.clear();
    }

    @Test
    @DisplayName("delta × N 之后收到 done：增量按序推送，onComplete 恰好一次")
    void translatesDeltaAndDone() throws IOException {
        HttpServer server = startServer(200, """
                event: meta
                data: {"runId":1,"detectedConcepts":["TLB"],"estimatedTokens":512}

                event: delta
                data: {"delta":"TLB "}

                event: delta
                data: {"delta":"是页表缓存"}

                event: done
                data: {"runId":1,"elapsedMs":120}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.deltas).containsExactly("TLB ", "是页表缓存");
        assertThat(handler.completes.get()).isEqualTo(1);
        assertThat(handler.errors).isEmpty();
        // meta 不产生任何回调，也不该被当成 delta 漏出去
        assertThat(handler.deltas).noneMatch(d -> d.contains("detectedConcepts"));
        assertThat(handler.completions.get(0).truncated()).isFalse();
    }

    @Test
    @DisplayName("done 携带 finishReason=length：保留 delta，但终态标记为截断")
    void translatesTruncatedDone() throws IOException {
        HttpServer server = startServer(200, """
                event: delta
                data: {"delta":"TLB 缓存的是页表项，"}

                event: done
                data: {"runId":1,"finishReason":"length","truncated":true}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        // 部分文本仍然送达，前端可以展示；但 onComplete 必须带截断元数据，
        // 不能把「被 Token 上限切断」压缩成一次普通正常结束。
        assertThat(handler.deltas).containsExactly("TLB 缓存的是页表项，");
        assertThat(handler.completes.get()).isEqualTo(1);
        assertThat(handler.errors).isEmpty();
        assertThat(handler.completions.get(0).truncated()).isTrue();
        assertThat(handler.completions.get(0).finishReason()).isEqualTo("length");
    }

    @Test
    @DisplayName("done 缺少 truncated 但 finishReason=length：仍按截断处理")
    void infersTruncationFromFinishReason() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":1,"finishReason":"length"}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.completes.get()).isEqualTo(1);
        assertThat(handler.completions.get(0).truncated()).isTrue();
    }

    @Test
    @DisplayName("done 明确写 truncated=false 但 finishReason=length：length 仍然优先")
    void finishReasonLengthOverridesContradictoryFlag() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":1,"finishReason":"length","truncated":false}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.completes.get()).isEqualTo(1);
        assertThat(handler.completions.get(0).truncated()).isTrue();
        assertThat(handler.completions.get(0).finishReason()).isEqualTo("length");
    }

    @Test
    @DisplayName("error 事件里的 int code 原样透传，不被翻译层替换成常量")
    void passesErrorCodeThrough() throws IOException {
        HttpServer server = startServer(200, """
                event: error
                data: {"runId":1,"code":503,"message":"后端检索不可用"}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.completes.get()).isZero();
        assertThat(handler.errors).hasSize(1);
        // 503 是 Agent 侧用来区分「我的后端挂了」的码；若被写成 500，这份分类就丢在内网了
        assertThat(handler.errors.get(0).code).isEqualTo(503);
        assertThat(handler.errors.get(0).message).isEqualTo("后端检索不可用");
    }

    @Test
    @DisplayName("done 之后再来的事件一律丢弃：终局仍恰好一次")
    void dropsEventsAfterTerminal() throws IOException {
        HttpServer server = startServer(200, """
                event: delta
                data: {"delta":"正文"}

                event: done
                data: {"runId":1}

                event: delta
                data: {"delta":"迟到的增量"}

                event: error
                data: {"runId":1,"code":500,"message":"迟到的错误"}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.deltas).containsExactly("正文");
        assertThat(handler.completes.get()).isEqualTo(1);
        assertThat(handler.errors).isEmpty();
    }

    @Test
    @DisplayName("流干净结束但没等到 done：按失败处理（半截回复不能落库）")
    void truncatedStreamBecomesError() throws IOException {
        HttpServer server = startServer(200, """
                event: delta
                data: {"delta":"这句话没说完"}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.deltas).containsExactly("这句话没说完");
        assertThat(handler.completes.get()).isZero();
        assertThat(handler.errors).hasSize(1);
        assertThat(handler.errors.get(0).code).isEqualTo(502);
    }

    @Test
    @DisplayName("上游返回 HTTP 500：用该状态码报错，不自己编")
    void mapsUpstreamHttpErrorStatus() throws IOException {
        HttpServer server = startServer(500, "{\"detail\":\"boom\"}");

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.completes.get()).isZero();
        assertThat(handler.errors).hasSize(1);
        assertThat(handler.errors.get(0).code).isEqualTo(500);
    }

    @Test
    @DisplayName("Agent 没起（连接被拒）：onError(503)，且不抛异常")
    void connectionRefusedBecomesUnavailable() throws IOException {
        // 借一个端口再释放，保证这个端口上确实没有进程在听
        int deadPort;
        try (ServerSocket socket = new ServerSocket(0)) {
            deadPort = socket.getLocalPort();
        }
        HttpPythonAgentGateway gateway =
                new HttpPythonAgentGateway("http://127.0.0.1:" + deadPort, 1000, 5000,
                        TEST_TOKEN, signer());

        RecordingHandler handler = new RecordingHandler();
        gateway.streamReply(sampleRequest(), handler);

        assertThat(handler.completes.get()).isZero();
        assertThat(handler.errors).hasSize(1);
        assertThat(handler.errors.get(0).code).isEqualTo(503);
    }

    @Test
    @DisplayName("请求体就是 ChatAgentRequest 本身：camelCase、含 authToken、方法与路径都对")
    void sendsRequestAsIs() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":7}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        // 字段名两边靠 alias 生成器对齐，这里锁住的就是那份契约：
        // 一旦 ChatAgentRequest 的字段改名而 Agent 侧没跟，这条断言先红。
        assertThat(lastRequestBody.get()).contains("\"runId\":7");
        assertThat(lastRequestBody.get()).contains("\"traceId\":\"" + TRACE_ID + "\"");
        assertThat(lastRequestBody.get()).contains("\"learningSpaceId\":3");
        assertThat(lastRequestBody.get()).contains("\"userMessage\":\"TLB Miss 是什么\"");
        assertThat(lastRequestBody.get()).contains("\"authToken\":\"jwt-token\"");
        // Session 是嵌套对象，Python 侧靠 ChatRequest.session 接住它 ——
        // 字段名一旦对不上，Agent 那边会静默丢掉整个「本阶段学习目标」区块
        assertThat(lastRequestBody.get()).contains("\"session\":{");
        assertThat(lastRequestBody.get()).contains("\"goal\":\"理解地址转换\"");
        assertThat(lastRequestBody.get()).contains("\"currentFocus\":\"卡在 TLB Miss\"");
        assertThat(lastRequestBody.get()).contains("\"role\":\"USER\"");
    }

    @Test
    @DisplayName("调用 Agent 必须携带内部凭据，并用请求 userId 绑定身份")
    void sendsInternalIdentityHeaders() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":7}

                """);

        gateway(server).streamReply(sampleRequest(), new RecordingHandler());

        // Agent 侧未配置 token 时 fail-closed 503；配置后无凭据是 401。
        // 这三个头缺任何一个，真实链路都会在鉴权层断掉。
        assertThat(lastTokenHeader.get()).isEqualTo(TEST_TOKEN);
        assertThat(lastUserHeader.get()).isEqualTo("1");
        assertThat(signer().verifyForAudience(lastIdentityHeader.get(), "learning-agent"))
                .as("身份声明必须由签名密钥签发且绑定 userId")
                .contains(1L);
    }

    @Test
    @DisplayName("未配置身份签名密钥时 fail closed：不调用 Agent，直接 onError(503)")
    void refusesToCallAgentWithoutIdentitySecret() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":7}

                """);
        HttpPythonAgentGateway gateway = new HttpPythonAgentGateway(
                "http://127.0.0.1:" + server.getAddress().getPort(), 2000, 10000,
                TEST_TOKEN, signer(""));

        RecordingHandler handler = new RecordingHandler();
        gateway.streamReply(sampleRequest(), handler);

        assertThat(handler.completes.get()).isZero();
        assertThat(handler.errors).hasSize(1);
        assertThat(handler.errors.get(0).code).isEqualTo(503);
        assertThat(requestCount.get()).as("签名失败时不得发出网络请求").isZero();
    }

    @Test
    @DisplayName("不得发出 h2c 升级请求：明文 HTTP 下带 Connection: Upgrade 会让 uvicorn 丢掉请求体")
    void doesNotSendH2cUpgrade() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":7}

                """);

        gateway(server).streamReply(sampleRequest(), new RecordingHandler());

        // JDK HttpClient 默认 version 是 HTTP_2，明文场景会先在请求里塞
        // "Connection: Upgrade, HTTP2-Settings" 去试探 h2c。uvicorn 不支持，
        // 只记一行 "Unsupported upgrade request" 就把请求体丢掉，FastAPI 随后回
        // 422 {"type":"missing","loc":["body"]} —— 这个 bug 在真机上才暴露过一次，
        // 所以在这里钉住：任何一次升级尝试都算回归。
        assertThat(lastConnectionHeader.get())
                .as("明文 HTTP 下不应出现 h2c 升级头")
                .doesNotContainIgnoringCase("Upgrade");
    }

    @Test
    @DisplayName("done 携带 citations：解析为结构化引用，畸形条目丢弃、重复条目去重")
    void parsesStructuredCitations() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":1,"citations":[{"documentId":10,"chunkId":1,"chunkIndex":0,"sourceTitle":"zenframe.md"},{"documentId":10,"chunkId":1,"chunkIndex":0,"sourceTitle":"重复条目"},{"documentId":11,"chunkId":2,"sourceTitle":"内存管理.md"},{"documentId":0,"chunkId":3,"sourceTitle":"非法文档"},{"documentId":12,"sourceTitle":"缺少 chunk"},{"documentId":13,"chunkId":4,"sourceTitle":"  "},{"documentId":"14","chunkId":5,"sourceTitle":"类型错误"}]}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.completions).hasSize(1);
        assertThat(handler.completions.get(0).citations()).containsExactly(
                new AgentCitation(10L, 1L, 0, "zenframe.md"),
                new AgentCitation(11L, 2L, null, "内存管理.md"));
    }

    @Test
    @DisplayName("done 没有 citations 字段：终态引用为空，旧 Agent 不因此失败")
    void missingCitationsIsCompatible() throws IOException {
        HttpServer server = startServer(200, """
                event: done
                data: {"runId":1}

                """);

        RecordingHandler handler = new RecordingHandler();
        gateway(server).streamReply(sampleRequest(), handler);

        assertThat(handler.completions).hasSize(1);
        assertThat(handler.completions.get(0).citations()).isEmpty();
    }

    // ---------- 脚手架 ----------

    private HttpPythonAgentGateway gateway(HttpServer server) {
        return new HttpPythonAgentGateway(
                "http://127.0.0.1:" + server.getAddress().getPort(), 2000, 10000,
                TEST_TOKEN, signer());
    }

    private static InternalIdentitySigner signer() {
        return signer(TEST_IDENTITY_SECRET);
    }

    private static InternalIdentitySigner signer(String secret) {
        return new InternalIdentitySigner(
                secret, secret + "-delegation", "learning-agent", 60,
                "learning-backend", 300, 5, 900, 3600);
    }

    /**
     * 起一个只会把 {@code body} 原样吐回来的假 Agent。
     * <p>
     * 用固定长度（而不是 chunked）发送，客户端读到字节数就自然结束 ——
     * 这恰好也是「流结束」与「收到 done」是两条独立路径的那个场景。
     */
    private HttpServer startServer(int status, String body) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/agent/chat", exchange -> {
            lastRequestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            lastConnectionHeader.set(String.valueOf(exchange.getRequestHeaders().getFirst("Connection")));
            lastTokenHeader.set(String.valueOf(exchange.getRequestHeaders().getFirst("X-Internal-Token")));
            lastUserHeader.set(String.valueOf(exchange.getRequestHeaders().getFirst("X-Internal-User-Id")));
            lastIdentityHeader.set(String.valueOf(exchange.getRequestHeaders().getFirst("X-Internal-Identity")));
            requestCount.incrementAndGet();
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream; charset=utf-8");
            exchange.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        });
        server.start();
        servers.add(server);
        return server;
    }

    private ChatAgentRequest sampleRequest() {
        return new ChatAgentRequest(
                7L,
                TRACE_ID,
                1L, 2L, 3L,
                "TLB Miss 是什么",
                List.of(new ChatHistoryMessage(MessageRole.USER, "上一轮的问题")),
                new ChatAgentRequest.SessionContext(20L, "虚拟内存", "理解地址转换", "卡在 TLB Miss"),
                "jwt-token");
    }

    /** 记录回调，用于断言「调用了几次、顺序对不对」。 */
    private static final class RecordingHandler implements AgentStreamHandler {

        private final List<String> deltas = new ArrayList<>();
        private final List<Error> errors = new ArrayList<>();
        private final List<StreamCompletion> completions = new ArrayList<>();
        private final AtomicInteger completes = new AtomicInteger();

        @Override
        public void onDelta(String delta) {
            deltas.add(delta);
        }

        @Override
        public void onComplete() {
            completes.incrementAndGet();
            completions.add(StreamCompletion.normal());
        }

        @Override
        public void onComplete(StreamCompletion completion) {
            completes.incrementAndGet();
            completions.add(completion);
        }

        @Override
        public void onError(int code, String message) {
            errors.add(new Error(code, message));
        }
    }

    private record Error(int code, String message) {
    }
}
