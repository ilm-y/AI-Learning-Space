package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.dto.document.agent.AgentIndexRequest;
import com.ails.backend.dto.document.agent.AgentIndexResponse;
import com.ails.backend.dto.document.agent.AgentParseResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.security.InternalIdentitySigner;
import com.ails.backend.utils.JsonUtil;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.ByteArrayOutputStream;
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
import java.util.UUID;

/**
 * Spring Boot → FastAPI 的文档解析/索引客户端。
 *
 * <p><b>不传磁盘路径</b>：原文通过 multipart 文件内容传输，FastAPI 不需要与
 * Spring Boot 共享文件系统。两个请求都带现有的
 * {@code X-Internal-Token} / {@code X-Internal-User-Id} 内部凭据。</p>
 *
 * <p><b>失败语义</b>：任何 HTTP 错误、鉴权失败、响应契约不合法都抛
 * {@link DocumentIndexException}；调用方据此把文档置为 FAILED，绝不吞掉错误
 * 或回退到假 Embedding。</p>
 */
@Slf4j
@Component
public class AgentDocumentClient {

    static final String TOKEN_HEADER = "X-Internal-Token";
    static final String USER_HEADER = "X-Internal-User-Id";
    static final String IDENTITY_HEADER = "X-Internal-Identity";

    private static final String PARSE_PATH = "/agent/documents/parse";
    private static final String INDEX_PATH = "/agent/documents/index";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final HttpClient client;
    private final String baseUrl;
    private final String internalToken;
    private final InternalIdentitySigner identitySigner;
    private final Duration parseTimeout;
    private final Duration indexTimeout;

    public AgentDocumentClient(
            @Value("${ails.agent.base-url}") String baseUrl,
            @Value("${ails.agent.connect-timeout-ms:3000}") long connectTimeoutMs,
            @Value("${ails.internal.token:}") String internalToken,
            DocumentIndexProperties properties,
            InternalIdentitySigner identitySigner) {
        this.baseUrl = trimTrailingSlash(baseUrl);
        this.internalToken = internalToken == null ? "" : internalToken.trim();
        this.identitySigner = identitySigner;
        this.parseTimeout = Duration.ofMillis(properties.parseTimeoutMs());
        this.indexTimeout = Duration.ofMillis(properties.indexTimeoutMs());
        this.client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .followRedirects(HttpClient.Redirect.NEVER)
                // 与现有 Agent 客户端一致：明文 HTTP 下钉死 1.1，避免 h2c 探测丢请求体。
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    public AgentParseResponse parse(Document document, byte[] content) {
        requireInternalToken();
        String boundary = "----ails-document-" + UUID.randomUUID().toString().replace("-", "");
        byte[] body = multipartBody(boundary, document, content);
        JsonNode data = send(baseUrl + PARSE_PATH,
                body,
                "multipart/form-data; boundary=" + boundary,
                parseTimeout,
                "解析",
                document.getUserId());
        try {
            return MAPPER.treeToValue(data, AgentParseResponse.class);
        } catch (Exception e) {
            throw new DocumentIndexException(502, "Agent 解析响应不符合契约", e);
        }
    }

    public AgentIndexResponse index(Document document, AgentIndexRequest request) {
        requireInternalToken();
        byte[] body = JsonUtil.toJson(request).getBytes(StandardCharsets.UTF_8);
        JsonNode data = send(baseUrl + INDEX_PATH, body, "application/json", indexTimeout, "索引",
                document.getUserId());
        try {
            return MAPPER.treeToValue(data, AgentIndexResponse.class);
        } catch (Exception e) {
            throw new DocumentIndexException(502, "Agent 索引响应不符合契约", e);
        }
    }

    // ===================== HTTP =====================

    private JsonNode send(String url, byte[] body, String contentType, Duration timeout,
                          String action, Long userId) {
        HttpRequest request;
        // 文档归属用户来自已认证的上传者（Document.userId），由服务端签发；
        // 缺密钥时 fail closed，避免文档以“无主”或可伪造身份进入索引。
        final String identity;
        try {
            identity = identitySigner.sign(userId);
        } catch (RuntimeException e) {
            throw new DocumentIndexException(503,
                    "未配置 ails.internal.identity-secret，无法签发内部身份", e);
        }
        try {
            request = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .timeout(timeout)
                    .header("Content-Type", contentType)
                    .header("Accept", "application/json")
                    .header(TOKEN_HEADER, internalToken)
                    .header(IDENTITY_HEADER, identity)
                    .header(USER_HEADER, String.valueOf(userId))
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                    .build();
        } catch (RuntimeException e) {
            throw new DocumentIndexException(500, "构造 Agent " + action + "请求失败", e);
        }
        return sendRequest(request, action);
    }

    private JsonNode sendRequest(HttpRequest request, String action) {
        HttpResponse<String> response;
        try {
            response = client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (HttpConnectTimeoutException | ConnectException e) {
            throw new DocumentIndexException(503, "Agent 服务未启动或无法连接", e);
        } catch (HttpTimeoutException e) {
            throw new DocumentIndexException(504, "等待 Agent " + action + "超时", e);
        } catch (IOException e) {
            throw new DocumentIndexException(503, "连接 Agent 失败", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new DocumentIndexException(500, "Agent " + action + "请求被中断", e);
        }

        if (response.statusCode() >= 400) {
            throw new DocumentIndexException(response.statusCode(),
                    "Agent " + action + "失败（HTTP " + response.statusCode() + "）："
                            + abbreviate(response.body(), 300));
        }
        try {
            JsonNode root = MAPPER.readTree(response.body());
            int code = root.path("code").asInt(-1);
            if (code != 0) {
                int status = code >= 400 && code < 600 ? code : 502;
                throw new DocumentIndexException(status,
                        "Agent " + action + "失败：" + root.path("message").asText("code=" + code));
            }
            JsonNode data = root.path("data");
            if (!data.isObject()) {
                throw new DocumentIndexException(502, "Agent " + action + "响应缺少 data");
            }
            return data;
        } catch (DocumentIndexException e) {
            throw e;
        } catch (Exception e) {
            throw new DocumentIndexException(502, "Agent " + action + "响应不是合法 JSON", e);
        }
    }

    // ===================== multipart =====================

    private byte[] multipartBody(String boundary, Document document, byte[] content) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(content.length + 1024);
        writeField(out, boundary, "documentId", String.valueOf(document.getId()));
        writeField(out, boundary, "learningSpaceId", String.valueOf(document.getLearningSpaceId()));
        writeField(out, boundary, "userId", String.valueOf(document.getUserId()));
        writeField(out, boundary, "fileName", document.getFileName());

        String fileName = headerSafe(document.getFileName());
        String fileContentType = "md".equalsIgnoreCase(document.getFileType())
                ? "text/markdown" : "text/plain";
        write(out, "--" + boundary + "\r\n");
        write(out, "Content-Disposition: form-data; name=\"file\"; filename=\"" + fileName + "\"\r\n");
        write(out, "Content-Type: " + fileContentType + "\r\n\r\n");
        out.writeBytes(content);
        write(out, "\r\n--" + boundary + "--\r\n");
        return out.toByteArray();
    }

    private static void writeField(ByteArrayOutputStream out, String boundary, String name, String value) {
        write(out, "--" + boundary + "\r\n");
        write(out, "Content-Disposition: form-data; name=\"" + name + "\"\r\n\r\n");
        write(out, value == null ? "" : value);
        write(out, "\r\n");
    }

    private static void write(ByteArrayOutputStream out, String value) {
        out.writeBytes(value.getBytes(StandardCharsets.UTF_8));
    }

    private static String headerSafe(String value) {
        if (value == null || value.isBlank()) {
            return "document.txt";
        }
        return value.replace("\"", "_").replace("\r", "").replace("\n", "");
    }

    private void requireInternalToken() {
        if (internalToken.isEmpty()) {
            throw new DocumentIndexException(503,
                    "未配置 ails.internal.token，无法调用 Agent 文档索引接口");
        }
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
