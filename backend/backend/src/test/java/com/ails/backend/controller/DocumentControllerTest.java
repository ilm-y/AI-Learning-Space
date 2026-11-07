package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import com.ails.backend.common.GlobalExceptionHandler;
import com.ails.backend.dto.document.DocumentResponse;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.dto.document.ReadyDocument;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.security.AuthUser;
import com.ails.backend.service.DocumentService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * Controller 路由/响应体测试：不启动 Spring 容器、不连接数据库。
 * 认证上下文直接写入 SecurityContext，模拟 JWT 过滤器已经建立的身份。
 */
@ExtendWith(MockitoExtension.class)
class DocumentControllerTest {

    @Mock
    private DocumentService documentService;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders
                .standaloneSetup(new DocumentController(documentService))
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        new AuthUser(1L, "tester", "token"),
                        null,
                        List.of(new SimpleGrantedAuthority("ROLE_USER"))));
    }

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    void uploadReturnsCreatedOrDuplicateFlag() throws Exception {
        when(documentService.upload(eq(1L), eq(10L), any()))
                .thenReturn(new DocumentUploadResponse(documentResponse(), false));

        mockMvc.perform(multipart("/api/spaces/10/documents")
                        .file(new MockMultipartFile("file", "notes.md", "text/markdown",
                                "# TLB".getBytes(StandardCharsets.UTF_8))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.message").value("上传成功"))
                .andExpect(jsonPath("$.data.duplicate").value(false))
                .andExpect(jsonPath("$.data.document.id").value(9))
                .andExpect(jsonPath("$.data.document.status").value("UPLOADING"));

        verify(documentService).upload(eq(1L), eq(10L), any());
    }

    @Test
    void uploadDuplicateUsesStableMessage() throws Exception {
        when(documentService.upload(eq(1L), eq(10L), any()))
                .thenReturn(new DocumentUploadResponse(documentResponse(), true));

        mockMvc.perform(multipart("/api/spaces/10/documents")
                        .file(new MockMultipartFile("file", "notes.md", "text/markdown",
                                "same".getBytes(StandardCharsets.UTF_8))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.message").value("文件已存在"))
                .andExpect(jsonPath("$.data.duplicate").value(true));
    }

    @Test
    void listsDocumentsOfSpace() throws Exception {
        when(documentService.listBySpace(1L, 10L)).thenReturn(List.of(documentResponse()));

        mockMvc.perform(get("/api/spaces/10/documents"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].id").value(9))
                .andExpect(jsonPath("$.data[0].fileType").value("md"));
    }

    @Test
    void getsDocumentById() throws Exception {
        when(documentService.get(1L, 9L)).thenReturn(documentResponse());

        mockMvc.perform(get("/api/documents/9"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(9))
                .andExpect(jsonPath("$.data.fileName").value("notes.md"));
    }

    @Test
    void otherUsersDocumentReturns404WithoutLeakingExistence() throws Exception {
        when(documentService.get(1L, 9L))
                .thenThrow(BusinessException.notFound("文档不存在"));

        mockMvc.perform(get("/api/documents/9"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404))
                .andExpect(jsonPath("$.message").value("文档不存在"));
    }

    @Test
    void readyIdsUsesAuthenticatedUserAndForwardsEmbeddingIdentity() throws Exception {
        when(documentService.listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai-compatible", 1024, "v1:abc"))
                .thenReturn(List.of(readyDocument(7L, 3), readyDocument(8L, 1)));

        mockMvc.perform(get("/api/spaces/10/documents/ready-ids")
                        .param("embeddingModel", "text-embedding-v4")
                        .param("embeddingProvider", "openai-compatible")
                        .param("embeddingDimension", "1024")
                        .param("embeddingFingerprint", "v1:abc"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].id").value(7))
                .andExpect(jsonPath("$.data[0].indexVersion").value(3))
                .andExpect(jsonPath("$.data[1].id").value(8))
                .andExpect(jsonPath("$.data[1].indexVersion").value(1));

        verify(documentService).listReadyDocuments(
                1L, 10L, "text-embedding-v4", "openai-compatible", 1024, "v1:abc");
    }

    @Test
    void readyIdsAllowsMissingEmbeddingModel() throws Exception {
        when(documentService.listReadyDocuments(1L, 10L, null, null, null, null))
                .thenReturn(List.of());

        mockMvc.perform(get("/api/spaces/10/documents/ready-ids"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data").isArray())
                .andExpect(jsonPath("$.data").isEmpty());

        verify(documentService).listReadyDocuments(1L, 10L, null, null, null, null);
    }

    @Test
    void reindexUsesAuthenticatedUser() throws Exception {
        when(documentService.reindex(1L, 9L)).thenReturn(documentResponse());

        mockMvc.perform(post("/api/documents/9/reindex"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.message").value("已提交重建"))
                .andExpect(jsonPath("$.data.id").value(9));

        verify(documentService).reindex(1L, 9L);
    }

    @Test
    void deleteUsesAuthenticatedUserAndIsIdempotentAtControllerLevel() throws Exception {
        mockMvc.perform(delete("/api/documents/9"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.message").value("已提交删除"));

        verify(documentService).delete(1L, 9L);
    }

    private static ReadyDocument readyDocument(long id, int version) {
        ReadyDocument ready = new ReadyDocument();
        ready.setId(id);
        ready.setIndexVersion(version);
        return ready;
    }

    private static DocumentResponse documentResponse() {
        return new DocumentResponse(
                9L, 10L, 1L, "notes", "notes.md", "md", 5L,
                "a".repeat(64), DocumentStatus.UPLOADING,
                0, 0, null, null, 0, null, null, null,
                null, null, null, null, null);
    }
}
