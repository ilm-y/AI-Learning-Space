package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.entity.Document;
import com.ails.backend.mapper.DocumentMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Objects;

/**
 * 非活跃索引版本的延迟清理。
 *
 * <p>索引切换成功后会立即尝试清理旧版本；如果那次清理失败（Agent 不可用、
 * 进程中断等），本任务会继续重试。清理失败不影响检索正确性：检索只按
 * {@code document.index_version} 对应的 {@code doc_version} 过滤。</p>
 *
 * <p><b>不会误删正在构建的版本：</b>扫描条件排除 {@code index_started_at IS NOT NULL}
 * 的文档（有活跃构建租约），因此构建中的 staged chunk 不会被当成“旧版本”。</p>
 */
@Slf4j
@Service
public class DocumentIndexCleanupService {

    private final DocumentMapper documentMapper;
    private final DocumentChunkService chunkService;
    private final AgentDocumentClient agentClient;
    private final DocumentIndexProperties properties;

    public DocumentIndexCleanupService(DocumentMapper documentMapper,
                                       DocumentChunkService chunkService,
                                       AgentDocumentClient agentClient,
                                       DocumentIndexProperties properties) {
        this.documentMapper = documentMapper;
        this.chunkService = chunkService;
        this.agentClient = agentClient;
        this.properties = properties;
    }

    @Scheduled(fixedDelayString = "${ails.document.index.cleanup-interval-ms:300000}",
            initialDelayString = "${ails.document.index.cleanup-initial-delay-ms:120000}")
    public void cleanupStaleVersions() {
        List<Long> ids;
        try {
            ids = documentMapper.selectDocumentsWithStaleVersions(properties.cleanupBatchSize());
        } catch (Exception e) {
            log.error("扫描旧索引版本失败", e);
            return;
        }
        for (Long id : ids) {
            try {
                cleanup(id);
            } catch (Exception e) {
                // 单个文档失败不阻塞其余文档；下一轮继续。
                log.warn("清理文档旧索引版本失败: documentId={}, error={}", id, e.toString());
            }
        }
    }

    void cleanup(Long documentId) {
        Document document = documentMapper.selectById(documentId);
        if (document == null || document.getIndexVersion() == null
                || document.getIndexStartedAt() != null) {
            return;
        }
        int activeVersion = document.getIndexVersion();
        int attempts = document.getIndexAttempts() == null ? 0 : document.getIndexAttempts();
        List<Integer> stale = chunkService.listOtherVersions(documentId, activeVersion);
        if (stale.isEmpty()) {
            return;
        }

        // 删除前重新校验：扫描与删除之间可能已有新 worker 抢到租约并写入更高版本。
        // 只删除“扫描时已存在且 <= 当时 attempt”的版本，避免把新版本列入删除清单。
        Document current = documentMapper.selectById(documentId);
        if (current == null || current.getIndexStartedAt() != null
                || !Objects.equals(current.getIndexVersion(), activeVersion)
                || !Objects.equals(current.getIndexAttempts(), attempts)) {
            log.warn("放弃清理旧索引版本：文档租约/活跃版本已变化, documentId={}, "
                            + "activeVersion={}, attempts={}",
                    documentId, activeVersion, attempts);
            return;
        }
        List<Integer> deletable = stale.stream()
                .filter(version -> version != null && version <= attempts)
                .toList();
        if (deletable.isEmpty()) {
            return;
        }
        // 先删 Agent 向量再删 MySQL chunk：Agent 失败时 chunk 记录仍在，下一轮
        // 扫描还能发现并重试；反过来会留下没有记录可定位的孤儿向量。
        agentClient.deleteVersions(document, deletable, deletable.contains(0),
                document.getVectorCollection());
        chunkService.deleteVersions(documentId, deletable);
        log.info("Cleaned stale index versions: documentId={}, activeVersion={}, stale={}",
                documentId, activeVersion, deletable);
    }
}
