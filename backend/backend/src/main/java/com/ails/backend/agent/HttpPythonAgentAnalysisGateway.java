package com.ails.backend.agent;

import com.ails.backend.dto.agent.AnalyzeAgentRequest;
import com.ails.backend.dto.agent.AnalyzeAgentResult;
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

/**
 * {@link AgentAnalysisGateway} 的真实实现：同步 POST {@code /agent/analyze}。
 * <p>
 * 与 {@link HttpPythonAgentGateway} 共享两条 JDK HttpClient 的硬性设置：
 * 显式 HTTP/1.1（明文下默认的 h2c 升级会让 uvicorn 丢掉请求体）与不跟随重定向。
 * 差别在于这里不需要解析 SSE —— Agent 返回的是 {@code {"code":0,"data":{...}}}。
 *
 * <h3>超时为什么单独配</h3>
 * 分析是一次 LLM {@code complete()} 调用，通常比流式回答短，但它发生在 SSE 关闭之后、
 * 仍然占着 {@code chatStreamExecutor} 的一条线程。沿用对话的 240 秒会让一次卡死的分析
 * 白白占住池子四分钟；默认 90 秒足够一次结构化输出，超时就退回规则版。
 */
@Slf4j
@Component
@ConditionalOnProperty(name = "ails.agent.gateway", havingValue = "http")
public class HttpPythonAgentAnalysisGateway implements AgentAnalysisGateway {

    private static final String ANALYZE_PATH = "/agent/analyze";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final HttpClient client;
    private final String analyzeUrl;
    private final Duration timeout;

    public HttpPythonAgentAnalysisGateway(
            @Value("${ails.agent.base-url}") String baseUrl,
            @Value("${ails.agent.connect-timeout-ms}") long connectTimeoutMs,
            @Value("${ails.agent.analyze-timeout-ms:90000}") long analyzeTimeoutMs) {
        String trimmed = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.analyzeUrl = trimmed + ANALYZE_PATH;
        this.timeout = Duration.ofMillis(analyzeTimeoutMs);
        this.client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .followRedirects(HttpClient.Redirect.NEVER)
                // 同 HttpPythonAgentGateway：明文 HTTP 下不钉死 1.1 会触发 h2c 探测
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    @Override
    public boolean enabled() {
        return true;
    }

    @Override
    public AnalyzeAgentResult analyze(AnalyzeAgentRequest request) {
        HttpRequest httpRequest;
        try {
            httpRequest = HttpRequest.newBuilder()
                    .uri(URI.create(analyzeUrl))
                    .timeout(timeout)
                    .header("Content-Type", "application/json")
                    .header("Accept", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(
                            JsonUtil.toJson(request), StandardCharsets.UTF_8))
                    .build();
        } catch (RuntimeException e) {
            // URI 非法 / 序列化失败都属于本地配置问题，不该被误报成 Agent 不可用
            throw new AgentAnalysisException(500, "构造分析请求失败", e);
        }

        HttpResponse<String> response;
        try {
            response = client.send(httpRequest, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (HttpConnectTimeoutException | ConnectException e) {
            log.warn("连接 Agent 分析接口失败, url={}, runId={}: {}", analyzeUrl, request.runId(), e.toString());
            throw new AgentAnalysisException(503, "Agent 服务未启动或无法连接", e);
        } catch (HttpTimeoutException e) {
            log.warn("等待 Agent 分析超时, url={}, runId={}", analyzeUrl, request.runId());
            throw new AgentAnalysisException(503, "等待 Agent 分析超时", e);
        } catch (IOException e) {
            log.warn("与 Agent 分析接口通信失败, url={}, runId={}", analyzeUrl, request.runId(), e);
            throw new AgentAnalysisException(503, "连接 Agent 失败", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new AgentAnalysisException(500, "分析请求被中断", e);
        }

        if (response.statusCode() >= 400) {
            log.error("Agent 分析返回异常状态, runId={}, status={}, body={}",
                    request.runId(), response.statusCode(), abbreviate(response.body()));
            throw new AgentAnalysisException(response.statusCode(),
                    "Agent 分析失败（HTTP " + response.statusCode() + "）");
        }

        return parse(response.body(), request.runId());
    }

    /**
     * 解析 {@code {"code":0,"data":{...}}}。
     * <p>
     * 任何一层不符合契约都按 502 处理：调用方会退回规则版，而不是把半份结果写进库。
     * 这里不做「字段缺失就猜」的兼容 —— Agent 侧的 schema 测试已经覆盖了字段名。
     */
    private AnalyzeAgentResult parse(String body, Long runId) {
        try {
            JsonNode root = MAPPER.readTree(body);
            int code = root.path("code").asInt(-1);
            if (code != 0) {
                throw new AgentAnalysisException(502,
                        "Agent 分析返回业务错误：" + root.path("message").asText("code=" + code));
            }
            JsonNode data = root.path("data");
            if (data.isMissingNode() || data.isNull() || !data.isObject()) {
                throw new AgentAnalysisException(502, "Agent 分析响应缺少 data");
            }
            return MAPPER.treeToValue(data, AnalyzeAgentResult.class);
        } catch (AgentAnalysisException e) {
            throw e;
        } catch (Exception e) {
            log.error("解析 Agent 分析响应失败, runId={}, body={}", runId, abbreviate(body), e);
            throw new AgentAnalysisException(502, "Agent 分析响应不符合契约", e);
        }
    }

    private static String abbreviate(String text) {
        if (text == null) {
            return "";
        }
        return text.length() <= 500 ? text : text.substring(0, 500) + "…";
    }
}
