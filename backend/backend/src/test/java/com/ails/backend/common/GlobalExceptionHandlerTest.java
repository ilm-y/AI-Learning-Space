package com.ails.backend.common;

import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 错误响应始终是可解析的 JSON，即使客户端只接受 {@code text/event-stream}。
 *
 * <p>SSE 接口（{@code POST /api/conversations/{id}/messages}）在建立流之前就可能失败：
 * 对话不属于当前用户、请求校验失败等。如果此时只按 Accept 做内容协商，
 * {@code @ExceptionHandler} 会因为“没有可接受的表示”而解析失败，最终被
 * Spring Security 的入口点改写成 401“未登录或 Token 已过期”——把 404 误导成认证失败。
 * 这里固定住“错误体强制 JSON”的契约。</p>
 */
class GlobalExceptionHandlerTest {

    private final MockMvc mockMvc = MockMvcBuilders
            .standaloneSetup(new FailingController())
            .setControllerAdvice(new GlobalExceptionHandler())
            .build();

    @Test
    void businessErrorIsJsonEvenWhenClientOnlyAcceptsEventStream() throws Exception {
        mockMvc.perform(get("/boom")
                        .accept(MediaType.TEXT_EVENT_STREAM))
                .andExpect(status().isNotFound())
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andExpect(jsonPath("$.code").value(404))
                .andExpect(jsonPath("$.message").value("对话不存在"));
    }

    @Test
    void unknownErrorIsJsonEvenWhenClientOnlyAcceptsEventStream() throws Exception {
        mockMvc.perform(get("/explode")
                        .accept(MediaType.TEXT_EVENT_STREAM))
                .andExpect(status().isInternalServerError())
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andExpect(jsonPath("$.code").value(500));
    }

    @RestController
    static class FailingController {

        @GetMapping("/boom")
        void boom() {
            throw BusinessException.notFound("对话不存在");
        }

        @GetMapping("/explode")
        void explode() {
            throw new IllegalStateException("boom");
        }
    }
}
