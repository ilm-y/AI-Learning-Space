package com.ails.backend.service;

import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.mapper.DocumentMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;

/**
 * 索引任务超时回收。
 *
 * <p><b>为什么需要它。</b> 索引任务由进程内线程池执行，跨 Agent / Embedding / Qdrant
 * 多个外部服务。进程被杀、机器重启、线程挂死时，document 行会永远停在 PROCESSING：
 * 它不会进 READY 白名单（不会污染检索），但用户会看到一个永远「处理中」的文档，
 * 运维也无法区分「还在跑」和「已经丢了」。</p>
 *
 * <p><b>三类回收，失败语义不同：</b></p>
 * <ul>
 *   <li><b>PROCESSING（首次索引）</b>：超时 → FAILED，可重新提交；</li>
 *   <li><b>READY + 活跃租约（重建）</b>：超时 → <b>保持 READY</b>，只释放租约并写错误。
 *       旧版本仍在服务，绝不能因为重建失败把文档置 FAILED；</li>
 *   <li><b>UPLOADING（上传后进程崩溃）</b>：未超过最大尝试次数则重新提交，否则 FAILED。</li>
 * </ul>
 *
 * <p><b>fencing：</b>所有回收 UPDATE 都自增 {@code index_attempts}，使旧 worker 的
 * complete / fail 因 attempt 不匹配而失效。重复扫描 / 多实例扫描只会成功一次。</p>
 */
@Slf4j
@Service
public class DocumentIndexRecoveryService {

    private static final int ERROR_MAX_LENGTH = 1024;

    private final DocumentMapper documentMapper;
    private final DocumentIndexService indexService;
    private final DocumentIndexProperties properties;

    public DocumentIndexRecoveryService(DocumentMapper documentMapper,
                                        DocumentIndexService indexService,
                                        DocumentIndexProperties properties) {
        this.documentMapper = documentMapper;
        this.indexService = indexService;
        this.properties = properties;
    }

    @Scheduled(
            fixedDelayString = "${ails.document.index.recovery-interval-ms:300000}",
            initialDelayString = "${ails.document.index.recovery-initial-delay-ms:60000}")
    public void recoverStale() {
        recoverStale(LocalDateTime.now());
    }

    /**
     * 执行一次回收。包可见，便于集成测试用可控时间点触发；生产路径由
     * {@link #recoverStale()} 定时调用。
     *
     * @return 本次被推进到终态/重新提交的文档数
     */
    int recoverStale(LocalDateTime now) {
        int changed = 0;
        changed += recoverStaleProcessing(now);
        changed += recoverStaleReadyRebuilds(now);
        changed += recoverStaleUploading(now);
        return changed;
    }

    private int recoverStaleProcessing(LocalDateTime now) {
        long timeoutMs = properties.staleProcessingTimeoutMs();
        LocalDateTime cutoff = now.minus(Duration.ofMillis(timeoutMs));
        List<Long> staleIds = documentMapper.selectStaleProcessingIds(
                cutoff, properties.recoveryBatchSize());
        if (staleIds.isEmpty()) {
            return 0;
        }

        String error = abbreviate(
                "索引任务超过 " + formatWindow(timeoutMs)
                        + " 仍处于 PROCESSING，已判定为中断并标记 FAILED；可重新提交索引",
                ERROR_MAX_LENGTH);
        int marked = 0;
        for (Long documentId : staleIds) {
            try {
                int updated = documentMapper.markStaleProcessingFailed(documentId, cutoff, error);
                if (updated == 1) {
                    marked++;
                    log.warn("回收超时索引任务: documentId={}, cutoff={}", documentId, cutoff);
                }
            } catch (RuntimeException e) {
                log.error("回收超时索引任务失败: documentId={}", documentId, e);
            }
        }
        log.info("PROCESSING 回收扫描完成: 命中 {} 条, 实际标记 FAILED {} 条, cutoff={}",
                staleIds.size(), marked, cutoff);
        return marked;
    }

    private int recoverStaleReadyRebuilds(LocalDateTime now) {
        long timeoutMs = properties.staleProcessingTimeoutMs();
        LocalDateTime cutoff = now.minus(Duration.ofMillis(timeoutMs));
        List<Long> staleIds = documentMapper.selectStaleReadyRebuildIds(
                cutoff, properties.recoveryBatchSize());
        if (staleIds.isEmpty()) {
            return 0;
        }

        String error = abbreviate(
                "重建任务超过 " + formatWindow(timeoutMs)
                        + " 仍占用租约，已放弃本次重建；文档保持 READY，旧索引继续服务",
                ERROR_MAX_LENGTH);
        int marked = 0;
        for (Long documentId : staleIds) {
            try {
                int updated = documentMapper.markStaleReadyRebuildAbandoned(
                        documentId, cutoff, error);
                if (updated == 1) {
                    marked++;
                    log.warn("放弃超时重建任务（文档保持 READY）: documentId={}, cutoff={}",
                            documentId, cutoff);
                }
            } catch (RuntimeException e) {
                log.error("回收超时重建任务失败: documentId={}", documentId, e);
            }
        }
        log.info("READY 重建回收扫描完成: 命中 {} 条, 实际释放 {} 条, cutoff={}",
                staleIds.size(), marked, cutoff);
        return marked;
    }

    private int recoverStaleUploading(LocalDateTime now) {
        LocalDateTime cutoff = now.minus(Duration.ofMillis(properties.uploadStaleTimeoutMs()));
        List<Long> staleIds = documentMapper.selectStaleUploadingIds(
                cutoff, properties.recoveryBatchSize());
        if (staleIds.isEmpty()) {
            return 0;
        }

        int resubmitted = 0;
        for (Long documentId : staleIds) {
            try {
                var document = documentMapper.selectById(documentId);
                if (document == null) {
                    continue;
                }
                int attempts = document.getIndexAttempts() == null ? 0 : document.getIndexAttempts();
                if (attempts >= properties.maxIndexAttempts()) {
                    documentMapper.markUploadingFailed(documentId, abbreviate(
                            "文档在 " + attempts + " 次尝试后仍未提交索引，请重新上传",
                            ERROR_MAX_LENGTH));
                    log.warn("UPLOADING 重试次数耗尽，标记 FAILED: documentId={}, attempts={}",
                            documentId, attempts);
                } else {
                    indexService.submit(documentId);
                    resubmitted++;
                    log.info("重新提交滞留的 UPLOADING 文档: documentId={}, attempts={}",
                            documentId, attempts);
                }
            } catch (RuntimeException e) {
                log.error("恢复 UPLOADING 文档失败: documentId={}", documentId, e);
            }
        }
        log.info("UPLOADING 回收扫描完成: 命中 {} 条, 重新提交 {} 条, cutoff={}",
                staleIds.size(), resubmitted, cutoff);
        return resubmitted;
    }

    private static String formatWindow(long timeoutMs) {
        if (timeoutMs >= 60_000L) {
            long minutes = timeoutMs / 60_000L;
            return minutes + " 分钟";
        }
        return timeoutMs + " 毫秒";
    }

    private static String abbreviate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
