package com.ails.backend.service;

import com.ails.backend.dto.document.agent.AgentIndexChunk;
import com.ails.backend.dto.document.agent.AgentIndexRequest;
import com.ails.backend.dto.document.agent.AgentIndexResponse;
import com.ails.backend.dto.document.agent.AgentParseResponse;
import com.ails.backend.dto.document.agent.AgentParsedChunk;
import com.ails.backend.dto.document.agent.AgentVectorRef;
import com.ails.backend.config.DocumentIndexProperties;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.task.TaskExecutor;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.RejectedExecutionException;

/**
 * 文档索引编排：分阶段构建、成功后原子切换、旧版本延迟清理。
 *
 * <p><b>顺序（与旧实现的关键差异）：</b></p>
 * <ol>
 *   <li>条件更新抢占构建租约，并把 {@code index_attempts} 作为本次构建令牌（fencing token）；</li>
 *   <li>读取原文，调用 Agent 解析；</li>
 *   <li>把新 chunk 写入新的 {@code index_version}，<b>不删除旧版本</b>；</li>
 *   <li>调用 Agent 生成 Embedding 并写入 Qdrant 的新版本 points；Agent 在返回前
 *       校验该版本 point 数，部分写入不会报告成功；</li>
 *   <li>写回本次版本的 chunk.vector_ref；</li>
 *   <li>一条条件 UPDATE 切换 {@code document.index_version = attempt} 并置 READY；
 *       失败则回滚，旧版本继续服务；</li>
 *   <li>切换成功后异步清理旧版本；清理失败不影响检索。</li>
 * </ol>
 *
 * <p><b>没有跨存储事务：</b>MySQL 是事实源，Qdrant 是派生存储。所有切换前写入都
 * 属于“非活跃版本”，不会被检索；只有 MySQL 的条件切换成功，新版本才生效。</p>
 */
@Slf4j
@Service
public class DocumentIndexService {

    private static final int MAX_INDEX_ERROR_LENGTH = 1024;
    private static final int MAX_CHUNKS = 10_000;

    private final DocumentMapper documentMapper;
    private final DocumentChunkService chunkService;
    private final DocumentStorageService storageService;
    private final AgentDocumentClient agentClient;
    private final DocumentIndexProperties properties;
    private final TaskExecutor executor;

    public DocumentIndexService(DocumentMapper documentMapper,
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
     * 提交后台索引。同步抢占租约：首次索引/失败重试进入 PROCESSING；
     * READY 重建保持 READY，让旧版本在构建期间继续服务。
     */
    public void submit(Long documentId) {
        if (documentId == null) {
            return;
        }
        Long attempt = acquireLease(documentId);
        if (attempt == null) {
            log.debug("文档不满足索引条件或已有 writer，跳过提交: documentId={}", documentId);
            return;
        }
        try {
            executor.execute(() -> runIndex(documentId, attempt));
        } catch (RejectedExecutionException e) {
            log.warn("文档索引线程池已满，直接置为 FAILED: documentId={}", documentId);
            fail(documentId, attempt, "索引任务队列已满，请稍后重试");
        }
    }

    /**
     * 抢占构建租约。条件更新是唯一的并发边界：
     * <ul>
     *   <li>UPLOADING / FAILED → PROCESSING；</li>
     *   <li>READY → 保持 READY（旧版本继续服务），只占租约；</li>
     *   <li>同一文档已有未超时租约时 0 行；</li>
     *   <li>每次成功抢占都自增 {@code index_attempts}，作为本次构建的版本号与 fencing token。</li>
     * </ul>
     *
     * @return 本次构建令牌；未抢到返回 {@code null}
     */
    Long acquireLease(Long documentId) {
        // 冷却窗口内不允许同一文档重复构建；首次索引/失败重试通常没有租约，直接可抢。
        LocalDateTime cutoff = LocalDateTime.now()
                .minus(Duration.ofMillis(properties.reindexCooldownMs()));
        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, documentId)
                .isNull(Document::getDeletedAt)
                .in(Document::getStatus,
                        DocumentStatus.UPLOADING, DocumentStatus.FAILED, DocumentStatus.READY)
                .and(w -> w.isNull(Document::getIndexStartedAt)
                        .or().lt(Document::getIndexStartedAt, cutoff))
                .setSql("status = CASE WHEN status = 'READY' THEN 'READY' ELSE 'PROCESSING' END")
                .setSql("index_attempts = index_attempts + 1")
                .set(Document::getIndexStartedAt, LocalDateTime.now())
                .set(Document::getIndexError, null));
        if (updated != 1) {
            return null;
        }
        // 读回本次令牌。只有抢到租约的 writer 会走到这里；若回收服务在两步之间
        // fence 了该行，下面的 runIndex 会因租约/attempt 不匹配而安全退出。
        Document document = documentMapper.selectById(documentId);
        if (document == null || document.getIndexStartedAt() == null) {
            return null;
        }
        return (long) document.getIndexAttempts();
    }

    /**
     * 实际索引流程。包可见以便集成测试直接触发；生产路径统一走 {@link #submit(Long)}。
     */
    void runIndex(Long documentId, long attempt) {
        Document document = documentMapper.selectById(documentId);
        if (!isLeaseOwner(document, attempt)) {
            log.warn("索引任务开始时租约已变化，放弃执行: documentId={}, attempt={}", documentId, attempt);
            return;
        }

        int version = Math.toIntExact(attempt);
        int previousVersion = document.getIndexVersion() == null ? 0 : document.getIndexVersion();
        ValidatedIndex validated;
        try {
            byte[] content = storageService.read(document.getStorageUri());
            AgentParseResponse parsed = agentClient.parse(document, content);
            List<AgentParsedChunk> chunks = validateParsedChunks(parsed);

            List<DocumentChunk> staged = chunkService.stageChunks(document, chunks, version);
            AgentIndexResponse indexed = agentClient.index(document, indexRequest(document, staged, version));
            validated = validateIndexResponse(staged, indexed);
            chunkService.updateVectorRefs(documentId, version, validated.refs());

            // 唯一的“生效”动作：条件切换。失败时旧 index_version 原样保留。
            complete(documentId, attempt, staged.size(), parsed.parserVersion(), validated);
            log.info("Document indexed: id={}, version={}, chunks={}, model={}, collection={}",
                    documentId, version, staged.size(),
                    validated.embeddingModel(), validated.collection());
        } catch (Exception e) {
            log.error("Document indexing failed: id={}, attempt={}", documentId, attempt, e);
            rollbackStagedVersion(document, version);
            fail(documentId, attempt, summarize(e));
            return;
        }

        // 切换已成功。清理失败不能反过来影响 READY 状态，也不能让旧 points 被检索
        // （检索按 doc_version 过滤）。下一轮清理任务会继续重试。
        try {
            cleanupOldVersions(document, version, previousVersion, validated.collection());
        } catch (Exception e) {
            log.warn("旧索引清理失败，稍后重试: documentId={}, activeVersion={}, error={}",
                    documentId, version, e.toString());
        }
    }

    private boolean isLeaseOwner(Document document, long attempt) {
        return document != null
                && document.getIndexStartedAt() != null
                && Objects.equals(document.getIndexAttempts(), Math.toIntExact(attempt))
                && (document.getStatus() == DocumentStatus.PROCESSING
                || document.getStatus() == DocumentStatus.READY);
    }

    /**
     * 失败时只清理本次 attempt 的 MySQL chunk 与 Qdrant points，旧活跃版本保持不动。
     */
    private void rollbackStagedVersion(Document document, int attempt) {
        boolean vectorsCleaned = false;
        try {
            // 保留文档当前活跃版本，删除其余版本（包括本次失败 attempt）。
            agentClient.deleteOtherVersions(document, document.getIndexVersion(), document.getVectorCollection());
            vectorsCleaned = true;
        } catch (Exception e) {
            log.warn("清理失败构建的 Qdrant points 失败: documentId={}, attempt={}",
                    document.getId(), attempt, e);
        }
        if (!vectorsCleaned) {
            // 向量未清掉时保留 staged chunk 记录：定时清理任务据此重新发现文档并重试，
            // 否则会留下无法定位的孤儿 points。
            return;
        }
        try {
            chunkService.deleteVersion(document.getId(), attempt);
        } catch (Exception e) {
            log.warn("清理失败构建的 chunk 失败: documentId={}, attempt={}", document.getId(), attempt, e);
        }
    }

    /**
     * 切换成功后清理旧版本。非活跃版本本就不参与检索，因此这里允许失败。
     */
    void cleanupOldVersions(Document document, int activeVersion, int previousVersion, String collection) {
        // 先清理 Agent 向量、再删 MySQL 旧 chunk：若 Agent 失败，chunk 记录仍在，
        // 定时清理任务还能重新发现这个文档并重试；反过来失败则会永久留下孤儿向量。
        agentClient.deleteOtherVersions(document, activeVersion, collection);
        chunkService.deleteOtherVersions(document.getId(), activeVersion);
        log.info("Cleaned old index versions: documentId={}, activeVersion={}, previousVersion={}",
                document.getId(), activeVersion, previousVersion);
    }

    // ===================== 状态 =====================

    void complete(Long documentId, long attempt, int chunkCount,
                  String parserVersion, ValidatedIndex indexed) {
        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, documentId)
                .eq(Document::getIndexAttempts, Math.toIntExact(attempt))
                .isNull(Document::getDeletedAt)
                .in(Document::getStatus, DocumentStatus.PROCESSING, DocumentStatus.READY)
                .set(Document::getStatus, DocumentStatus.READY)
                .set(Document::getParserVersion, abbreviate(parserVersion, 64))
                .set(Document::getChunkCount, chunkCount)
                .set(Document::getEmbeddingModel, abbreviate(indexed.embeddingModel(), 128))
                .set(Document::getEmbeddingProvider, abbreviate(indexed.embeddingProvider(), 64))
                .set(Document::getEmbeddingDimension, indexed.embeddingDimension())
                .set(Document::getEmbeddingFingerprint, abbreviate(indexed.embeddingFingerprint(), 128))
                .set(Document::getVectorCollection, abbreviate(indexed.collection(), 255))
                .set(Document::getIndexVersion, Math.toIntExact(attempt))
                .set(Document::getIndexStartedAt, null)
                .set(Document::getIndexedAt, LocalDateTime.now())
                .set(Document::getIndexError, null));
        if (updated != 1) {
            throw new DocumentIndexException(409, "文档状态已变化，索引结果未标记为 READY");
        }
    }

    /**
     * 失败收敛：
     * <ul>
     *   <li>首次索引（PROCESSING）→ FAILED；</li>
     *   <li>READY 重建 → 保持 READY，只记录错误并释放租约，旧版本继续服务。</li>
     * </ul>
     * 条件带 {@code index_attempts}，被删除/回收/新构建 fence 后不会覆盖。
     */
    void fail(Long documentId, long attempt, String error) {
        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, documentId)
                .eq(Document::getIndexAttempts, Math.toIntExact(attempt))
                .isNull(Document::getDeletedAt)
                .in(Document::getStatus, DocumentStatus.PROCESSING, DocumentStatus.READY)
                .setSql("status = CASE WHEN status = 'PROCESSING' THEN 'FAILED' ELSE status END")
                .set(Document::getIndexError, abbreviate(error, MAX_INDEX_ERROR_LENGTH))
                .set(Document::getIndexStartedAt, null));
        if (updated != 1) {
            log.warn("无法写入文档失败状态（可能已删除/状态或 attempt 已变化）: documentId={}, attempt={}, error={}",
                    documentId, attempt, abbreviate(error, 300));
        }
    }

    // ===================== 契约校验 =====================

    private List<AgentParsedChunk> validateParsedChunks(AgentParseResponse parsed) {
        if (parsed == null || parsed.chunks() == null || parsed.chunks().isEmpty()) {
            throw new DocumentIndexException(422, "解析结果为空，无法建立索引");
        }
        if (parsed.chunks().size() > MAX_CHUNKS) {
            throw new DocumentIndexException(422, "文档块数量超过上限 " + MAX_CHUNKS);
        }

        List<AgentParsedChunk> normalized = new ArrayList<>(parsed.chunks().size());
        for (int i = 0; i < parsed.chunks().size(); i++) {
            AgentParsedChunk chunk = parsed.chunks().get(i);
            if (chunk == null) {
                throw new DocumentIndexException(502, "解析响应包含空 chunk");
            }
            if (chunk.chunkIndex() == null || chunk.chunkIndex() != i) {
                throw new DocumentIndexException(502,
                        "chunk_index 必须从 0 连续递增，期望 " + i + "，实际 " + chunk.chunkIndex());
            }
            if (chunk.content() == null || chunk.content().isBlank()) {
                throw new DocumentIndexException(502, "解析响应包含空 chunk 内容: index=" + i);
            }
            int tokenCount = chunk.tokenCount() == null
                    ? estimateTokens(chunk.content()) : chunk.tokenCount();
            if (tokenCount < 0) {
                throw new DocumentIndexException(502, "chunk token_count 不能为负数: index=" + i);
            }
            boolean estimated = chunk.tokenCount() == null
                    || Boolean.TRUE.equals(chunk.tokenCountEstimated());
            normalized.add(new AgentParsedChunk(
                    chunk.chunkIndex(),
                    chunk.content(),
                    tokenCount,
                    estimated,
                    chunk.metadata() == null ? Map.of() : chunk.metadata()));
        }
        return normalized;
    }

    private AgentIndexRequest indexRequest(Document document, List<DocumentChunk> chunks, int version) {
        List<AgentIndexChunk> payload = chunks.stream()
                .map(chunk -> new AgentIndexChunk(
                        chunk.getId(),
                        chunk.getChunkIndex(),
                        chunk.getContent(),
                        parseMetadata(chunk.getMetadataJson())))
                .toList();
        return new AgentIndexRequest(
                document.getId(),
                document.getUserId(),
                document.getLearningSpaceId(),
                document.getName(),
                document.getFileName(),
                document.getFileType(),
                version,
                payload);
    }

    private ValidatedIndex validateIndexResponse(List<DocumentChunk> saved, AgentIndexResponse indexed) {
        if (indexed == null || indexed.collection() == null || indexed.collection().isBlank()) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少 collection");
        }
        if (indexed.collection().length() > 255) {
            throw new DocumentIndexException(502, "collection 超过 255 字符");
        }
        if (indexed.embeddingModel() == null || indexed.embeddingModel().isBlank()) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少 embeddingModel");
        }
        if (indexed.embeddingModel().length() > 128) {
            throw new DocumentIndexException(502, "embeddingModel 超过 128 字符");
        }
        if (indexed.embeddingProvider() == null || indexed.embeddingProvider().isBlank()) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少 embeddingProvider");
        }
        if (indexed.embeddingProvider().length() > 64) {
            throw new DocumentIndexException(502, "embeddingProvider 超过 64 字符");
        }
        if (indexed.embeddingDimension() == null || indexed.embeddingDimension() <= 0) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少合法 embeddingDimension");
        }
        if (indexed.embeddingFingerprint() == null || indexed.embeddingFingerprint().isBlank()) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少 embeddingFingerprint");
        }
        if (indexed.embeddingFingerprint().length() > 128) {
            throw new DocumentIndexException(502, "embeddingFingerprint 超过 128 字符");
        }
        if (indexed.vectors() == null || indexed.vectors().size() != saved.size()) {
            throw new DocumentIndexException(502,
                    "向量引用数量与 chunk 数量不一致: chunks=" + saved.size()
                            + ", vectors=" + (indexed.vectors() == null ? 0 : indexed.vectors().size()));
        }

        Set<Long> expected = new HashSet<>();
        saved.forEach(chunk -> expected.add(chunk.getId()));
        Set<Long> seen = new HashSet<>();
        Map<Long, String> refs = new HashMap<>();
        for (AgentVectorRef ref : indexed.vectors()) {
            if (ref == null || ref.chunkId() == null || ref.pointId() == null
                    || ref.pointId().isBlank()) {
                throw new DocumentIndexException(502, "Agent 返回了空向量引用");
            }
            if (ref.pointId().length() > 255) {
                throw new DocumentIndexException(502, "Qdrant point ID 超过 255 字符");
            }
            if (!expected.contains(ref.chunkId())) {
                throw new DocumentIndexException(502, "Agent 返回了未知 chunkId: " + ref.chunkId());
            }
            if (!seen.add(ref.chunkId())) {
                throw new DocumentIndexException(502, "Agent 返回了重复 chunkId: " + ref.chunkId());
            }
            refs.put(ref.chunkId(), ref.pointId());
        }
        if (refs.size() != expected.size()) {
            throw new DocumentIndexException(502, "Agent 未返回全部 chunk 的向量引用");
        }
        return new ValidatedIndex(
                refs,
                indexed.collection(),
                indexed.embeddingModel(),
                indexed.embeddingProvider(),
                indexed.embeddingDimension(),
                indexed.embeddingFingerprint());
    }

    record ValidatedIndex(
            Map<Long, String> refs,
            String collection,
            String embeddingModel,
            String embeddingProvider,
            int embeddingDimension,
            String embeddingFingerprint) {
    }

    private static Map<String, Object> parseMetadata(String json) {
        if (json == null || json.isBlank()) {
            return Map.of();
        }
        try {
            return com.ails.backend.utils.JsonUtil.toMap(json);
        } catch (Exception e) {
            return Map.of("raw", json);
        }
    }

    private static int estimateTokens(String content) {
        return Math.max(1, (int) Math.ceil(content.length() / 4.0));
    }

    private static String summarize(Exception e) {
        if (e instanceof DocumentIndexException) {
            return e.getMessage();
        }
        StringBuilder summary = new StringBuilder(e.getClass().getSimpleName());
        if (e.getMessage() != null && !e.getMessage().isBlank()) {
            summary.append(": ").append(e.getMessage());
        }
        // 数据层异常常常只有根因里才有可读信息（例如 Data too long）。
        Throwable cause = e.getCause();
        int depth = 0;
        while (cause != null && depth++ < 3) {
            summary.append(" | ").append(cause.getClass().getSimpleName());
            if (cause.getMessage() != null && !cause.getMessage().isBlank()) {
                summary.append(": ").append(cause.getMessage());
            }
            cause = cause.getCause();
        }
        return summary.toString();
    }

    private static String abbreviate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
