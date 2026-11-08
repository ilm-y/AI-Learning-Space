package com.ails.backend.service;

import com.ails.backend.config.DocumentReconciliationProperties;
import com.ails.backend.dto.document.ReconcilableDocument;
import com.ails.backend.dto.document.agent.AgentVectorCountResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.mapper.DocumentChunkMapper;
import com.ails.backend.mapper.DocumentMapper;
import com.ails.backend.mapper.ScheduledTaskLeaseMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

/**
 * RAG-03：Qdrant → MySQL 反向对账。
 *
 * <p><b>为什么需要它：</b>{@code selectReadyDocuments} 只检查 MySQL 状态与 chunk 数。
 * 如果 Qdrant collection 被删除或 points 意外丢失，MySQL 仍会把文档作为 READY
 * 交付检索，造成长期静默失效。本任务定期用 COUNT 对比“MySQL 活跃版本 chunk 数”
 * 与“Qdrant 同 document_id + index_version 的 point 数”。</p>
 *
 * <p><b>对账方案（选 A，轻量 COUNT）：</b>只做 exact COUNT，不逐条 scroll chunk_id。
 * 理由：COUNT 是单次 RPC、成本与文档数线性相关；逐条存在性校验会产生 O(chunks)
 * 次/批查询，且对“数量相同但 point 内容错位”这类故障依然无法证明正确性。
 * 逐条校验可在 COUNT 不一致后按需人工触发，不作为常规路径。</p>
 *
 * <p><b>失败语义：</b></p>
 * <ul>
 *   <li>{@code MISMATCH}：Qdrant 可达，但数量不同（包括 collection 已被删除）；</li>
 *   <li>{@code UNREACHABLE}：Qdrant/Agent 不可达或超时。<b>不代表数据缺失</b>，
 *       绝不能据此把文档标记为异常；</li>
 *   <li>{@code UNLOCATABLE}：legacy 文档没有 collection 记录，无法对账；</li>
 *   <li>{@code OK}：数量一致。</li>
 * </ul>
 *
 * <p>所有结果只写入观测列，<b>不修改 {@code document.status}</b>：一次网络抖动
 * 不能把 READY 改坏。多实例通过 {@code scheduled_task_lease} 互斥。单轮只扫描
 * {@code batchSize} 个文档，但按 {@code vector_reconciliation_checked_at} 最久
 * 未检查优先排序，因此超过批量大小的文档也会在多轮中轮转覆盖。</p>
 */
@Slf4j
@Service
@ConditionalOnProperty(prefix = "ails.document.reconcile", name = "enabled",
        havingValue = "true", matchIfMissing = true)
public class DocumentVectorReconciliationService {

    /** 租约任务名；所有实例必须一致。 */
    static final String TASK_NAME = "document-vector-reconciliation";

    static final String STATUS_OK = "OK";
    static final String STATUS_MISMATCH = "MISMATCH";
    static final String STATUS_UNREACHABLE = "UNREACHABLE";
    static final String STATUS_UNLOCATABLE = "UNLOCATABLE";

    private static final int DETAIL_MAX_LENGTH = 512;

    private final DocumentMapper documentMapper;
    private final DocumentChunkMapper chunkMapper;
    private final DocumentIndexVersionService versionService;
    private final AgentDocumentClient agentClient;
    private final ScheduledTaskLeaseMapper leaseMapper;
    private final DocumentReconciliationProperties properties;

    public DocumentVectorReconciliationService(DocumentMapper documentMapper,
                                               DocumentChunkMapper chunkMapper,
                                               DocumentIndexVersionService versionService,
                                               AgentDocumentClient agentClient,
                                               ScheduledTaskLeaseMapper leaseMapper,
                                               DocumentReconciliationProperties properties) {
        this.documentMapper = documentMapper;
        this.chunkMapper = chunkMapper;
        this.versionService = versionService;
        this.agentClient = agentClient;
        this.leaseMapper = leaseMapper;
        this.properties = properties;
    }

    @Scheduled(
            fixedDelayString = "${ails.document.reconcile.interval-ms:3600000}",
            initialDelayString = "${ails.document.reconcile.initial-delay-ms:300000}")
    public void scheduledReconcile() {
        try {
            reconcileOnce();
        } catch (Exception e) {
            // 定时任务不能把异常抛给调度器；下一轮继续。
            log.warn("反向对账任务整体失败: {}", e.toString());
        }
    }

    /**
     * 执行一轮对账。包可见，便于单元/集成测试直接触发。
     *
     * @return 本轮发现的不一致文档数
     */
    int reconcileOnce() {
        LocalDateTime now = LocalDateTime.now();
        String owner = "reconcile-" + UUID.randomUUID();
        if (!acquireLease(owner, now)) {
            log.debug("反向对账租约被其他实例持有，跳过本轮: task={}", TASK_NAME);
            return 0;
        }
        try {
            List<ReconcilableDocument> documents =
                    documentMapper.selectReconcilableDocuments(properties.batchSize());
            int mismatches = 0;
            for (ReconcilableDocument document : documents) {
                try {
                    if (reconcileDocument(document, now)) {
                        mismatches++;
                    }
                } catch (Exception e) {
                    // 单个文档失败不阻塞其余文档；结果已按 UNREACHABLE 记录。
                    log.warn("反向对账处理文档失败: documentId={}, error={}",
                            document.id(), e.toString());
                }
            }
            log.info("反向对账完成: scanned={}, mismatches={}", documents.size(), mismatches);
            return mismatches;
        } finally {
            releaseLease(owner);
        }
    }

    private boolean acquireLease(String owner, LocalDateTime now) {
        long ttlMs = properties.leaseTtlMs();
        int changed = leaseMapper.tryAcquire(TASK_NAME, owner, now,
                now.plus(Duration.ofMillis(ttlMs)));
        // ON DUPLICATE KEY UPDATE 在“未过期、无变化”时返回 0；新建/抢占返回 >0。
        // 再复核 owner，防止极端竞争下两个实例都认为自己抢到。
        return changed > 0 && owner.equals(leaseMapper.selectOwner(TASK_NAME));
    }

    private void releaseLease(String owner) {
        try {
            leaseMapper.release(TASK_NAME, owner);
        } catch (RuntimeException e) {
            // 释放失败只会让租约在 TTL 后自然过期，不影响正确性。
            log.debug("释放反向对账租约失败（将在 TTL 后过期）: {}", e.toString());
        }
    }

    /**
     * @return 是否发现“Qdrant 可达但数量不一致”
     */
    private boolean reconcileDocument(ReconcilableDocument source, LocalDateTime checkedAt) {
        Integer indexVersion = source.indexVersion();
        if (indexVersion == null) {
            return false;
        }
        int expected = chunkMapper.countActiveVectorRefs(source.id(), indexVersion);
        String collection = resolveCollection(source);
        if (collection == null) {
            // legacy 边界：不猜测 collection 名，也不把“无法对账”当成数据异常。
            String detail = detail(expected, null, "<unlocatable>", null);
            recordStatus(source, indexVersion, STATUS_UNLOCATABLE, checkedAt, detail);
            log.warn("reconciliation=unlocatable, documentId={}, spaceId={}, expected={}, "
                            + "actual=<unknown>, collection=<unlocatable>",
                    source.id(), source.spaceId(), expected);
            return false;
        }

        AgentVectorCountResponse response;
        try {
            Document document = new Document();
            document.setId(source.id());
            document.setUserId(source.userId());
            response = agentClient.countPoints(document, collection, indexVersion);
        } catch (DocumentIndexException e) {
            // 503/504 = 连接失败/超时；其它 5xx 同样没有拿到可信计数。
            // 关键：UNREACHABLE 不是 MISMATCH，不能据此标记文档异常。
            String detail = detail(expected, null, collection, e.getMessage());
            recordStatus(source, indexVersion, STATUS_UNREACHABLE, checkedAt, detail);
            log.warn("reconciliation=unreachable, documentId={}, spaceId={}, expected={}, "
                            + "actual=<unreachable>, collection={}, error={}",
                    source.id(), source.spaceId(), expected, collection, e.toString());
            return false;
        }

        int actual = response.collectionExists() ? Math.toIntExact(response.count()) : 0;
        boolean consistent = response.collectionExists() && actual == expected;
        if (consistent) {
            recordStatus(source, indexVersion, STATUS_OK, checkedAt,
                    detail(expected, actual, collection, null));
            // 用 INFO 输出 OK，确保结构化对账日志在默认日志级别下也可见；
            // 单轮最多 batchSize 条，不会造成高频刷屏。
            log.info("reconciliation=ok, documentId={}, spaceId={}, expected={}, actual={}, "
                            + "collection={}",
                    source.id(), source.spaceId(), expected, actual, collection);
            return false;
        }

        recordStatus(source, indexVersion, STATUS_MISMATCH, checkedAt,
                detail(expected, actual, collection,
                        response.collectionExists() ? null : "collection does not exist"));
        log.warn("reconciliation=mismatch, documentId={}, spaceId={}, expected={}, actual={}, "
                        + "collection={}, collectionExists={}",
                source.id(), source.spaceId(), expected, actual, collection,
                response.collectionExists());
        return true;
    }

    private String resolveCollection(ReconcilableDocument source) {
        String recorded = versionService.findCollection(source.id(), source.indexVersion());
        String collection = recorded != null && !recorded.isBlank()
                ? recorded : source.vectorCollection();
        return collection == null || collection.isBlank() ? null : collection;
    }

    private void recordStatus(ReconcilableDocument source, Integer indexVersion,
                              String status, LocalDateTime checkedAt, String detail) {
        try {
            documentMapper.updateVectorReconciliation(source.id(), indexVersion, status,
                    checkedAt, abbreviate(detail));
        } catch (RuntimeException e) {
            // 观测写入失败不能影响对账主流程。
            log.warn("写入反向对账状态失败: documentId={}, status={}, error={}",
                    source.id(), status, e.toString());
        }
    }

    private static String detail(int expected, Integer actual, String collection, String error) {
        StringBuilder text = new StringBuilder()
                .append("expected=").append(expected)
                .append(", actual=").append(actual == null ? "<unknown>" : actual)
                .append(", collection=").append(collection);
        if (error != null && !error.isBlank()) {
            text.append(", error=").append(error);
        }
        return text.toString();
    }

    private static String abbreviate(String detail) {
        if (detail == null) {
            return null;
        }
        return detail.length() <= DETAIL_MAX_LENGTH
                ? detail : detail.substring(0, DETAIL_MAX_LENGTH);
    }
}
