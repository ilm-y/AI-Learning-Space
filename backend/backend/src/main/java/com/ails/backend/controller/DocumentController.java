package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.document.DocumentResponse;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.service.DocumentService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MaxUploadSizeExceededException;
import org.springframework.web.multipart.MultipartException;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;

/**
 * 文档上传、查询与 Agent 检索白名单。
 * <p>
 * 上传后由 {@code DocumentIndexService} 调 Agent 完成解析、切块、Embedding 与
 * Qdrant 写入；状态从 {@code UPLOADING} 经 {@code PROCESSING} 到 {@code READY}。
 * 本类不直接访问向量库，也不决定检索权限；{@code ready-ids} 只把当前用户、
 * 当前 Space 内索引完整的文档 ID 交给 Agent。
 */
@RestController
@RequiredArgsConstructor
public class DocumentController {

    private final DocumentService documentService;

    /**
     * multipart 上传。字段名固定为 {@code file}；客户端文件名只作为展示名。
     */
    @PostMapping("/api/spaces/{spaceId}/documents")
    public ApiResponse<DocumentUploadResponse> upload(
            @PathVariable Long spaceId,
            @RequestParam(value = "file", required = false) MultipartFile file) {
        DocumentUploadResponse result = documentService.upload(
                SecurityUtil.currentUserId(), spaceId, file);
        return ApiResponse.ok(result.duplicate() ? "文件已存在" : "上传成功", result);
    }

    @GetMapping("/api/spaces/{spaceId}/documents")
    public ApiResponse<List<DocumentResponse>> list(@PathVariable Long spaceId) {
        return ApiResponse.ok(documentService.listBySpace(
                SecurityUtil.currentUserId(), spaceId));
    }

    /**
     * Agent 知识检索使用的文档白名单。只返回当前用户、当前 Space 内
     * 真正 READY 且索引完整的文档；可选 {@code embeddingModel} 用于排除
     * 旧模型索引的文档，避免拿当前模型的查询向量去检索旧 collection。
     */
    @GetMapping("/api/spaces/{spaceId}/documents/ready-ids")
    public ApiResponse<List<Long>> readyIds(
            @PathVariable Long spaceId,
            @RequestParam(value = "embeddingModel", required = false) String embeddingModel) {
        return ApiResponse.ok(documentService.listReadyDocumentIds(
                SecurityUtil.currentUserId(), spaceId, embeddingModel));
    }

    @GetMapping("/api/documents/{id}")
    public ApiResponse<DocumentResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(documentService.get(SecurityUtil.currentUserId(), id));
    }

    /**
     * Spring 在进入方法前就可能因 multipart 上限抛异常，全局兜底会变成 500。
     * 这里在 Controller 内把它稳定映射为 413。
     */
    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<ApiResponse<Void>> handleMaxUploadSize(MaxUploadSizeExceededException ex) {
        return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE)
                .body(ApiResponse.fail(413, "文件超过大小上限"));
    }

    @ExceptionHandler(MultipartException.class)
    public ResponseEntity<ApiResponse<Void>> handleMultipart(MultipartException ex) {
        return ResponseEntity.badRequest()
                .body(ApiResponse.fail(400, "multipart 请求格式错误"));
    }
}
