package com.ails.backend.agent;

import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.security.InternalIdentitySigner;
import com.ails.backend.utils.JsonUtil;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
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
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Stream;

/**
 * {@link AgentGateway} 的真实实现：把一轮提问发给 Python Agent，把对方流回来的
 * SSE 事件翻译成 {@link AgentStreamHandler} 的回调。
 * <p>
 * 接通它只需要在配置里把 {@code ails.agent.gateway} 设成 {@code http}，
 * {@code ChatController} / {@code ChatService} 一行都不用改 —— 这正是
 * {@link AgentGateway} 这层抽象存在的意义。
 *
 * <h3>为什么用 JDK 自带的 HttpClient</h3>
 * 这个类是全仓唯一一处对外 HTTP 调用，而 {@code pom.xml} 里没有任何 HTTP 客户端依赖，
 * 本地 {@code ~/.m2} 也没有 {@code spring-webflux}，构建又要能离线跑（{@code scripts/e2e-verify.sh}
 * 用的是 {@code ./mvnw -o}）。{@code java.net.http.HttpClient} 是 JDK 17 自带的，
 * 零依赖、离线可构建，而且 {@code BodyHandlers.ofLines()} 恰好给出「一行一行读上游流」
 * 的天然形态 —— SSE 本身就是行协议，不需要为它引一个客户端库。
 * <p>
 * 上游的 SSE 只用到 {@code event:} / {@code data:} 两个字段（Agent 侧不发明文协议里的
 * {@code id:} / {@code retry:}），所以这里用一个约三十行的解析器而不是引库，是可以维护的。
 *
 * <h3>事件翻译表</h3>
 * <table border="1">
 *   <tr><th>Agent 事件</th><th>本类动作</th></tr>
 *   <tr><td>{@code meta}</td><td>只记日志（命中概念、上下文规模），无对应回调</td></tr>
 *   <tr><td>{@code delta}</td><td>{@link AgentStreamHandler#onDelta(String)}</td></tr>
 *   <tr><td>{@code done}</td><td>{@link AgentStreamHandler#onComplete(StreamCompletion)}：
 *       {@code finishReason=length} / {@code truncated=true} 时保留部分内容但标记不完整</td></tr>
 *   <tr><td>{@code error}</td><td>{@link AgentStreamHandler#onError(int, String)}</td></tr>
 * </table>
 * {@code error} 的 {@code code} <b>原样透传</b>事件里的 int，不在这里另编一套常量：
 * Agent 侧已经用 502 区分「模型上游挂了」、503 区分「后端检索不可用」，
 * 翻译层再覆盖一遍等于把这份分类丢在内网这一跳上，前端就只能看到笼统的失败。
 *
 * <h3>失败映射</h3>
 * <ul>
 *   <li>连不上 / 建连超时 / 总时长超时 → {@code 503}</li>
 *   <li>上游返回 HTTP ≥ 400 → 原样用该状态码</li>
 *   <li>流读了一半断掉、或流干净地结束但始终没等到 {@code done}/{@code error} → {@code 502}</li>
 * </ul>
 * 「干净地结束却没终局事件」必须当成失败：那意味着回复只推了一半，
 * 若当成 {@code onComplete} 就会把半截话落库成一条正常的助手消息。
 *
 * <h3>线程与超时</h3>
 * 本实现全程在调用方线程上跑（{@link AgentGateway} 契约第 1、2 条），不另开线程池。
 * 调用方 {@code ChatService} 的 {@code chatStreamExecutor} 核心线程只有 16 个，
 * 所以「一个不响应的 Agent 会占住一条线程直到超时」这件事要有兜底 ——
 * 兜底就是 {@code ails.agent.read-timeout-ms}。注意 {@code HttpRequest.timeout()}
 * 是<b>整段交换的总时长上限</b>而不是空闲超时，当前 {@code llm_max_tokens=1024}
 * 的生成量远够不着这个上限，所以先按总时长控制；真要按「多久没吐字」掐，
 * 得自己在消费循环里按 chunk 记空闲时间。
 */
@Slf4j
@Component
@ConditionalOnProperty(name = "ails.agent.gateway", havingValue = "http")
public class HttpPythonAgentGateway implements AgentGateway {

    /** Agent 的对话入口，见 {@code agent/app/main.py}。 */
    private static final String CHAT_PATH = "/agent/chat";

    /** 上游没给出可用 code 时的兜底（只用于事件本身畸形的情况，正常路径一律透传上游的 int）。 */
    private static final int CODE_BAD_GATEWAY = 502;
    private static final int CODE_UNAVAILABLE = 503;
    private static final int CODE_INTERNAL = 500;

    /**
     * 与 Agent 内部服务鉴权、后端 InternalServiceAuthenticationFilter 共用同一组头。
     * {@code X-Internal-User-Id} 只用于交叉核对，真正的用户身份在
     * {@code X-Internal-Identity} 签名声明里。
     */
    private static final String TOKEN_HEADER = "X-Internal-Token";
    private static final String USER_HEADER = "X-Internal-User-Id";
    private static final String IDENTITY_HEADER = "X-Internal-Identity";

    /** 解析事件 data 用。与 {@link JsonUtil} 里那个分开：这里只读不写，且不需要 java.time 支持。 */
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final HttpClient client;
    private final String chatUrl;
    private final Duration readTimeout;
    private final String internalToken;
    private final InternalIdentitySigner identitySigner;

    public HttpPythonAgentGateway(
            @Value("${ails.agent.base-url}") String baseUrl,
            @Value("${ails.agent.connect-timeout-ms}") long connectTimeoutMs,
            @Value("${ails.agent.read-timeout-ms}") long readTimeoutMs,
            @Value("${ails.internal.token:}") String internalToken,
            InternalIdentitySigner identitySigner) {
        // 防御性去掉结尾斜杠，避免拼出 //agent/chat（Starlette 会 307 重定向，白白多一跳）
        String trimmed = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.chatUrl = trimmed + CHAT_PATH;
        this.readTimeout = Duration.ofMillis(readTimeoutMs);
        this.internalToken = internalToken == null ? "" : internalToken.trim();
        this.identitySigner = identitySigner;
        this.client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                // 不跟随重定向：Agent 的地址是我们自己配的，跟着 3xx 跑只会在排查时把人带偏
                .followRedirects(HttpClient.Redirect.NEVER)
                // 必须钉死 HTTP/1.1。默认值是 HTTP_2，而明文（非 TLS）下 JDK 会先发一个
                // h2c 升级请求（带 Connection: Upgrade 头）试探对方 —— uvicorn 不支持这个，
                // 只会记一行 "Unsupported upgrade request" 然后**把请求体丢掉**，
                // FastAPI 于是收到空 body 并回 422 {"type":"missing","loc":["body"]}。
                // 表现是「Agent 明明活着、路径也对，却一直 422」，从两端日志都很难一眼看出，
                // 所以这里显式写死并留注释。
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    @Override
    public void streamReply(ChatAgentRequest request, AgentStreamHandler handler) {
        // 终局开关。契约要求 onComplete / onError 恰好一次，而「读循环自然结束」
        // 与「收到 done/error」是两条独立的路径，没有这个开关就会两头都收尾。
        AtomicBoolean terminated = new AtomicBoolean(false);

        // 身份声明必须由服务端已认证的 userId 签发。缺失/未配置时 fail closed，
        // 绝不降级成只带共享 token —— 那会让 Agent 无法区分用户。
        String identity;
        try {
            identity = identitySigner.sign(request.userId());
        } catch (RuntimeException e) {
            log.error("签发 Agent 内部身份失败, runId={}, userId={}",
                    request.runId(), request.userId(), e);
            emitError(handler, terminated, CODE_UNAVAILABLE, "内部身份签名不可用");
            return;
        }

        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(chatUrl))
                    .timeout(readTimeout)
                    .header("Content-Type", "application/json")
                    .header("Accept", "text/event-stream");
            if (!internalToken.isEmpty()) {
                builder.header(TOKEN_HEADER, internalToken);
            }
            builder.header(IDENTITY_HEADER, identity);
            // 保留明文 userId 仅供 Agent 交叉核对；它本身不再构成身份。
            builder.header(USER_HEADER, String.valueOf(request.userId()));
            HttpRequest httpRequest = builder
                    .POST(HttpRequest.BodyPublishers.ofString(
                            JsonUtil.toJson(request), StandardCharsets.UTF_8))
                    .build();

            HttpResponse<Stream<String>> response;
            try {
                response = client.send(httpRequest, HttpResponse.BodyHandlers.ofLines());
            } catch (HttpConnectTimeoutException | ConnectException e) {
                // Agent 进程没起是最常见的一种：这时候的错误信息要直接说清楚，别让人去翻栈
                log.warn("连接 Agent 失败, url={}, runId={}: {}", chatUrl, request.runId(), e.toString());
                emitError(handler, terminated, CODE_UNAVAILABLE, "Agent 服务未启动或无法连接");
                return;
            } catch (HttpTimeoutException e) {
                log.warn("等待 Agent 超时, url={}, runId={}", chatUrl, request.runId());
                emitError(handler, terminated, CODE_UNAVAILABLE, "等待 Agent 响应超时");
                return;
            } catch (IOException e) {
                log.warn("与 Agent 通信失败, url={}, runId={}", chatUrl, request.runId(), e);
                emitError(handler, terminated, CODE_UNAVAILABLE, "连接 Agent 失败");
                return;
            } catch (InterruptedException e) {
                // 恢复中断位：这个线程是池子里的，吞掉中断标记会让后续任务也失去取消能力
                Thread.currentThread().interrupt();
                log.warn("等待 Agent 时被中断, runId={}", request.runId());
                emitError(handler, terminated, CODE_INTERNAL, "请求已取消");
                return;
            }

            if (response.statusCode() >= 400) {
                // 上游把错误写在响应体里（FastAPI 的 422 就是这样），记下来便于排查；
                // 但给用户看的说明保持通用 —— 内部字段名不该出现在前端的报错里。
                log.error("Agent 返回异常状态, runId={}, status={}, body={}",
                        request.runId(), response.statusCode(), drain(response.body()));
                emitError(handler, terminated, response.statusCode(),
                        "Agent 处理失败（HTTP " + response.statusCode() + "）");
                return;
            }

            consume(response.body(), handler, terminated);

            // 流读完了却没有终局事件 —— 半截回复，按失败处理（见类注释）。
            emitError(handler, terminated, CODE_BAD_GATEWAY, "Agent 响应中断，回复不完整");
        } catch (Exception e) {
            // 兜住一切：包括 handler 自己抛出来的（调用方那侧的落库失败等）。
            // 契约第 4 条要求这里不往外抛 —— 抛出去只会把调用方的栈打成故障日志。
            log.error("HttpPythonAgentGateway failed, runId={}", request.runId(), e);
            emitError(handler, terminated, CODE_INTERNAL, "生成回复失败");
        }
    }

    // ---------- SSE 解析 ----------

    /**
     * 逐行读上游 SSE，遇到空行就派发一条完整事件。
     * <p>
     * 上游每条事件都是 {@code event: X\ndata: {...}\n\n}，没有跨行 data，
     * 所以按「空行分隔」而不是按「一行一条事件」解析 —— 后者在真出现多行 data 时会静默截断。
     */
    private void consume(Stream<String> lines, AgentStreamHandler handler, AtomicBoolean terminated) {
        String eventName = null;
        StringBuilder data = new StringBuilder();

        // (Iterable) 转换是为了能用增强 for：逐行消费，不在内存里攒整个响应
        for (String line : (Iterable<String>) lines::iterator) {
            if (terminated.get()) {
                // done/error 之后上游理论上不会再发东西；真发了就丢掉，
                // 不能让它变成「终局后又吐一个 delta」这种违反契约的行为。
                break;
            }
            if (line.isEmpty()) {
                dispatch(eventName, data.toString(), handler, terminated);
                eventName = null;
                data.setLength(0);
                continue;
            }
            if (line.startsWith(":")) {
                continue; // 注释行（心跳/keep-alive），SSE 协议里本就要忽略
            }
            int colon = line.indexOf(':');
            String field = colon < 0 ? line : line.substring(0, colon);
            String value = colon < 0 ? "" : line.substring(colon + 1);
            if (value.startsWith(" ")) {
                value = value.substring(1); // SSE 规定字段名后最多一个空格不算内容
            }
            switch (field) {
                case "event" -> eventName = value;
                case "data" -> {
                    if (data.length() > 0) {
                        data.append('\n');
                    }
                    data.append(value);
                }
                default -> {
                    // id: / retry: 用不到，忽略
                }
            }
        }
    }

    /** 把一条完整事件翻译成回调。事件畸形时只记日志不中断整条流 —— 丢掉一个字好过整轮失败。 */
    private void dispatch(String eventName, String data, AgentStreamHandler handler, AtomicBoolean terminated) {
        if (eventName == null || data.isEmpty() || terminated.get()) {
            return;
        }
        JsonNode node;
        try {
            node = MAPPER.readTree(data);
        } catch (Exception e) {
            log.warn("Agent 事件 data 不是合法 JSON, event={}, data={}", eventName, data);
            return;
        }
        switch (eventName) {
            case "meta" -> log.debug("Agent meta, runId={}, concepts={}, estimatedTokens={}",
                    node.path("runId").asLong(),
                    node.path("detectedConcepts"),
                    node.path("estimatedTokens").asInt());
            case "delta" -> {
                String delta = node.path("delta").asText("");
                if (!delta.isEmpty()) {
                    handler.onDelta(delta);
                }
            }
            case "done" -> {
                String finishReason = node.path("finishReason").isTextual()
                        ? node.path("finishReason").asText()
                        : null;
                // finishReason=length 是截断的硬事实：即使上游漏发或错发了
                // truncated=false，也不能把这一轮当成正常完成。
                boolean truncated = "length".equals(finishReason)
                        || node.path("truncated").asBoolean(false);
                finish(handler, terminated, new StreamCompletion(finishReason, truncated));
            }
            case "error" -> finish(handler, terminated, false,
                    // 正常路径下这个 int 由上游给出；asInt 的默认值只在事件本身畸形时才会生效
                    node.path("code").asInt(CODE_BAD_GATEWAY),
                    node.path("message").asText("Agent 生成失败"));
            default -> log.debug("忽略未知事件: {}", eventName);
        }
    }

    /** 正常终局：把完成原因一起交给调用方，截断不能被压缩成「正常完成」。 */
    private void finish(AgentStreamHandler handler, AtomicBoolean terminated,
                        StreamCompletion completion) {
        if (!terminated.compareAndSet(false, true)) {
            return;
        }
        handler.onComplete(completion);
    }

    /** 抢占终局名额；没抢到说明终局已经写过，直接丢弃（契约第 3 条）。 */
    private void finish(AgentStreamHandler handler, AtomicBoolean terminated,
                        boolean ok, int code, String message) {
        if (!terminated.compareAndSet(false, true)) {
            return;
        }
        if (ok) {
            handler.onComplete();
        } else {
            handler.onError(code, message);
        }
    }

    private void emitError(AgentStreamHandler handler, AtomicBoolean terminated, int code, String message) {
        finish(handler, terminated, false, code, message);
    }

    /** 把错误响应的前几行读出来用于日志。只用于失败路径，正常路径不会走到这里。 */
    private String drain(Stream<String> lines) {
        try (lines) {
            return lines.limit(3).reduce((a, b) -> a + "\n" + b).orElse("");
        } catch (Exception e) {
            return "<读取响应体失败>";
        }
    }
}
