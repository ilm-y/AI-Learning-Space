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
 * PROCESSING 索引任务的超时回收。
 *
 * <p><b>为什么需要它。</b> 索引任务由进程内线程池执行，跨 Agent / Embedding / Qdrant
 * 多个外部服务。进程被杀、机器重启、线程挂死时，document 行会永远停在 PROCESSING：
 * 它不会进 READY 白名单（不会污染检索），但用户会看到一个永远「处理中」的文档，
 * 运维也无法区分「还在跑」和「已经丢了」。</p>
 *
 * <p><b>为什么用 updated_at 而不是新增字段。</b> 进入 PROCESSING 的条件更新会刷新
 * {@code updated_at}（MyBatis-Plus 填充与 MySQL {@code ON UPDATE} 双保险），索引运行
 * 期间不再更新 document 行，因此它足以近似任务启动时间。只有在未来出现「处理期间
 * 合法更新 document 行」的路径时，才需要新增独立的 processing_started_at。</p>
 *
 * <p><b>安全边界。</b></p>
 * <ul>
 *   <li>只处理超时阈值之外的行；正常任务启动后会立即刷新 updated_at，不会被扫到；</li>
 *   <li>UPDATE 带 {@code status='PROCESSING'} 与同一个 cutoff 条件，与索引任务自己的
 *       complete / fail 在同一行锁上竞争；谁先提交谁赢，不会覆盖 READY；</li>
 *   <li>重复扫描 / 多实例扫描只会成功更新一次，其余更新 0 行（幂等）；</li>
 *   <li><b>不自动重试。</b> 回收只把状态收敛到 FAILED 并写可诊断错误；现有契约里
 *       FAILED 可由用户重新提交索引，不在后台静默重试。</li>
 *   <li>不是启动时无条件清理：由定时扫描按时间阈值触发。</li>
 * </ul>
 */
@Slf4j
@Service
public class DocumentIndexRecoveryService {

    private static final int ERROR_MAX_LENGTH = 1024;

    private final DocumentMapper documentMapper;
    private final DocumentIndexProperties properties;

    public DocumentIndexRecoveryService(DocumentMapper documentMapper,
                                        DocumentIndexProperties properties) {
        this.documentMapper = documentMapper;
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
     * @return 实际转为 FAILED 的文档数（并发竞争失败的行不计）
     */
    int recoverStale(LocalDateTime now) {
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
                int updated = documentMapper.markStaleProcessingFailed(
                        documentId, cutoff, error);
                if (updated == 1) {
                    marked++;
                    log.warn("回收超时索引任务: documentId={}, cutoff={}", documentId, cutoff);
                }
            } catch (RuntimeException e) {
                // 单条失败不阻断其余文档；下一轮扫描还会再次尝试。
                log.error("回收超时索引任务失败: documentId={}", documentId, e);
            }
        }
        log.info("索引任务回收扫描完成: 命中 {} 条, 实际标记 FAILED {} 条, cutoff={}",
                staleIds.size(), marked, cutoff);
        return marked;
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
