package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.entity.Document;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.List;
import java.util.concurrent.RejectedExecutionException;

/**
 * 文档删除的后台清理。
 *
 * <p><b>检索先停、清理后做：</b>请求删除时先用一条条件 UPDATE 把状态切到
 * {@code DELETING} 并 fence 正在运行的索引任务；检索白名单只接受 {@code READY}，
 * 因此从这一刻起文档不再参与召回。Qdrant / MySQL chunk / 文件的物理清理异步执行，
 * 失败保持 {@code DELETING} 并定时重试。</p>
 *
 * <p><b>为什么 DELETED 放在最后：</b>只有在 Qdrant 清理成功后才会软删并置
 * {@code DELETED}；否则会出现“文档不可见但向量仍可被检索”的窗口
 * （例如 Agent 使用旧的 ready 白名单缓存）。</p>
 *
 * <p><b>幂等：</b>清理流程每一步都可重复执行：Qdrant 按 document_id 删除、
 * MySQL chunk 按 document_id 删除、文件删除失败只记日志、状态更新带条件。</p>
 */
@Slf4j
@Service
public class DocumentDeletionService {

    private static final int MAX_DELETE_ERROR_LENGTH = 1024;

    private final DocumentMapper documentMapper;
    private final DocumentChunkService chunkService;
    private final DocumentStorageService storageService;
    private final AgentDocumentClient agentClient;
    private final DocumentIndexProperties properties;
    private final TaskExecutor executor;

    public DocumentDeletionService(DocumentMapper documentMapper,
                                   DocumentChunkService chunkService,
                                   DocumentStorageService storageService,
                                   AgentDocumentClient agentClient,
                                   DocumentIndexProperties properties,
                                   @Qualifier("documentIndexExecutor") TaskExecutor executor) {
        this.documentMapper = documentMapper;
        this.chunkService = chunkService;
        this.storageService = storageService;
        this.agentClient = agentClient;
        this.properties = properties;
        this.executor = executor;
    }

    /**
     * 请求删除：同步切换 DELETING（退出检索），异步清理。重复调用幂等。
     */
    public void requestDelete(Document document) {
        if (document.getStatus() == DocumentStatus.DELETED || document.getDeletedAt() != null) {
            return;
        }
        if (document.getStatus() == DocumentStatus.DELETING) {
            // 已经在删除中：再排一次清理，覆盖上次进程崩溃/线程未执行的情况。
            enqueue(document.getId());
            return;
        }

        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, document.getId())
                .isNull(Document::getDeletedAt)
                .in(Document::getStatus,
                        DocumentStatus.UPLOADING, DocumentStatus.PROCESSING,
                        DocumentStatus.READY, DocumentStatus.FAILED)
                .set(Document::getStatus, DocumentStatus.DELETING)
                // fence 正在运行的索引任务：其 complete/fail 因 attempt 不匹配而失效。
                .setSql("index_attempts = index_attempts + 1")
                .set(Document::getIndexStartedAt, null)
                .set(Document::getDeleteError, null));
        if (updated == 1) {
            enqueue(document.getId());
            return;
        }

        Document current = documentMapper.selectAnyById(document.getId());
        if (current != null && current.getStatus() == DocumentStatus.DELETING) {
            enqueue(document.getId());
        }
        // 其它情况（并发置 DELETED / 行已不存在）按幂等成功处理。
    }

    /**
     * 定时重试所有仍处于 DELETING 的文档。
     */
    @Scheduled(fixedDelayString = "${ails.document.index.delete-retry-interval-ms:60000}",
            initialDelayString = "${ails.document.index.delete-retry-initial-delay-ms:30000}")
    public void retryPendingDeletes() {
        List<Long> ids;
        try {
            ids = documentMapper.selectDeletingIds(properties.deleteMaxAttempts(), properties.deleteBatchSize());
        } catch (Exception e) {
            log.error("扫描待删除文档失败", e);
            return;
        }
        for (Long id : ids) {
            enqueue(id);
        }
    }

    /**
     * 执行清理。包可见以便测试直接触发确定性流程。
     */
    void cleanup(Long documentId) {
        Document document = documentMapper.selectAnyById(documentId);
        if (document == null
                || document.getDeletedAt() != null
                || document.getStatus() != DocumentStatus.DELETING) {
            return;
        }

        try {
            // 1) Qdrant：按 document_id 删除全部版本；collection 不存在也视为成功。
            agentClient.deleteDocument(document);
            // 2) MySQL chunk：可重复删除。
            chunkService.deleteAll(documentId);
            // 3) 文件：deleteQuietly 自身吞掉 IO 异常并记录路径。
            storageService.deleteQuietly(document.getStorageUri());
        } catch (Exception e) {
            log.warn("文档删除清理失败，保留 DELETING 等待重试: documentId={}, error={}",
                    documentId, e.toString());
            documentMapper.recordDeleteFailure(documentId, summarize(e));
            return;
        }

        int updated = documentMapper.markDeleted(documentId, LocalDateTime.now());
        if (updated == 1) {
            log.info("Document deleted: id={}", documentId);
        } else {
            // 清理已成功，但状态被并发修改（例如人工恢复）。不覆盖用户操作。
            log.warn("删除清理完成但状态更新未命中: documentId={}", documentId);
        }
    }

    private void enqueue(Long documentId) {
        try {
            executor.execute(() -> cleanup(documentId));
        } catch (RejectedExecutionException e) {
            // 队列满：保持 DELETING，下一轮定时任务会重试。
            log.warn("删除清理队列已满，稍后重试: documentId={}", documentId);
        }
    }

    private static String summarize(Exception e) {
        StringBuilder summary = new StringBuilder(e.getClass().getSimpleName());
        if (e.getMessage() != null && !e.getMessage().isBlank()) {
            summary.append(": ").append(e.getMessage());
        }
        Throwable cause = e.getCause();
        int depth = 0;
        while (cause != null && depth++ < 3) {
            summary.append(" | ").append(cause.getClass().getSimpleName());
            if (cause.getMessage() != null && !cause.getMessage().isBlank()) {
                summary.append(": ").append(cause.getMessage());
            }
            cause = cause.getCause();
        }
        String text = summary.toString();
        return text.length() <= MAX_DELETE_ERROR_LENGTH
                ? text : text.substring(0, MAX_DELETE_ERROR_LENGTH);
    }
}
