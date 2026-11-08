package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.document.DocumentResponse;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.dto.document.LegacyReadyReport;
import com.ails.backend.dto.document.ReadyDocument;
import com.ails.backend.service.DocumentService;
import com.ails.backend.utils.SecurityUtil;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
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
     * 真正 READY 且索引完整的文档，并带上<b>活跃索引版本</b>。
     *
     * <p>Embedding 身份参数由 Agent 传入当前真实生效的配置；provider/model/
     * dimension/fingerprint 不兼容的文档会被排除，绝不拿新模型的查询向量去
     * 检索旧模型写入的 collection/points。</p>
     *
     * <p><b>严格指纹模式（默认关闭）</b>开启后，本端点要求 {@code embeddingModel}
     * 与 {@code embeddingFingerprint} 同时非空，否则返回 400。它是<b>正常检索</b>
     * 的资格判定入口，不承担诊断职责；legacy 文档清单请使用下面的
     * {@code /diagnostics/legacy}。</p>
     */
    @GetMapping("/api/spaces/{spaceId}/documents/ready-ids")
    public ApiResponse<List<ReadyDocument>> readyIds(
            @PathVariable Long spaceId,
            @RequestParam(value = "embeddingModel", required = false) String embeddingModel,
            @RequestParam(value = "embeddingProvider", required = false) String embeddingProvider,
            @RequestParam(value = "embeddingDimension", required = false) Integer embeddingDimension,
            @RequestParam(value = "embeddingFingerprint", required = false) String embeddingFingerprint) {
        return ApiResponse.ok(documentService.listReadyDocuments(
                SecurityUtil.currentUserId(), spaceId,
                embeddingModel, embeddingProvider, embeddingDimension, embeddingFingerprint));
    }

    /**
     * <b>只读诊断端点</b>：列出当前用户、当前 Space 内 fingerprint 为空的 READY 文档。
     *
     * <p>这是 RAG-04 阶段 A 的观测/重建清单入口，<b>不是</b> Agent 的检索白名单。
     * 它与 {@code ready-ids} 刻意隔离：正常检索永远走 model + fingerprint → 严格白名单；
     * 诊断查询只供管理员/owner 排查和阶段 B 重建使用，不能被 Agent 检索流程消费。</p>
     *
     * <p>权限：与 Space 内其他接口一致，先校验 Space 归属（非 owner 返回 404），
     * SQL 再按 userId + spaceId 过滤，不会暴露其他用户的数据。</p>
     */
    @GetMapping("/api/spaces/{spaceId}/documents/diagnostics/legacy")
    public ApiResponse<LegacyReadyReport> legacyDiagnostics(
            @PathVariable Long spaceId,
            @RequestParam(value = "embeddingModel", required = false) String embeddingModel) {
        return ApiResponse.ok(documentService.legacyReadyDiagnostics(
                SecurityUtil.currentUserId(), spaceId, embeddingModel));
    }

    @GetMapping("/api/documents/{id}")
    public ApiResponse<DocumentResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(documentService.get(SecurityUtil.currentUserId(), id));
    }

    /**
     * 重建索引。READY 文档在构建期间保持 READY：新版本完整写入并校验成功后才切换，
     * 失败则继续使用旧版本。
     */
    @PostMapping("/api/documents/{id}/reindex")
    public ApiResponse<DocumentResponse> reindex(@PathVariable Long id) {
        return ApiResponse.ok("已提交重建",
                documentService.reindex(SecurityUtil.currentUserId(), id));
    }

    /**
     * 删除文档。状态先切 DELETING（立即退出检索白名单），向量与文件异步清理；
     * 重复删除幂等成功。
     */
    @DeleteMapping("/api/documents/{id}")
    public ApiResponse<Void> delete(@PathVariable Long id) {
        documentService.delete(SecurityUtil.currentUserId(), id);
        return ApiResponse.ok("已提交删除", null);
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
