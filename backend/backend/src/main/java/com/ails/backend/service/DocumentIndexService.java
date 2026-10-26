package com.ails.backend.service;

import com.ails.backend.dto.document.agent.AgentIndexChunk;
import com.ails.backend.dto.document.agent.AgentIndexRequest;
import com.ails.backend.dto.document.agent.AgentIndexResponse;
import com.ails.backend.dto.document.agent.AgentParseResponse;
import com.ails.backend.dto.document.agent.AgentParsedChunk;
import com.ails.backend.dto.document.agent.AgentVectorRef;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.task.TaskExecutor;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.RejectedExecutionException;

/**
 * 文档索引编排：UPLOADING → PROCESSING → READY / FAILED。
 *
 * <p>Spring Boot 负责业务状态、MySQL chunk 和事务一致性；FastAPI 负责解析、
 * Embedding 与 Qdrant 写入。跨服务没有分布式事务，因此采用可重试、幂等的步骤：</p>
 *
 * <ol>
 *   <li>条件更新抢占索引权（只有 UPLOADING/FAILED 能进入 PROCESSING）；</li>
 *   <li>读取原文，调用 Agent 解析；</li>
 *   <li>事务内替换 MySQL chunk（旧块删除 + 新块插入）；</li>
 *   <li>调用 Agent 索引，Qdrant 按 document_id 覆盖该文档的 points；</li>
 *   <li>写回 chunk.vector_ref；</li>
 *   <li>最后一步才条件更新 READY。</li>
 * </ol>
 *
 * <p>任何一步失败都会把状态置为 FAILED 并记录截断后的错误摘要。Qdrant 已写入但
 * 最后 MySQL 更新失败时，文档也不会被报告为 READY；重试会再次按 document_id
 * 覆盖，不会无限堆积 points。</p>
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
    private final TaskExecutor executor;

    public DocumentIndexService(DocumentMapper documentMapper,
                                DocumentChunkService chunkService,
                                DocumentStorageService storageService,
                                AgentDocumentClient agentClient,
                                @Qualifier("documentIndexExecutor") TaskExecutor executor) {
        this.documentMapper = documentMapper;
        this.chunkService = chunkService;
        this.storageService = storageService;
        this.agentClient = agentClient;
        this.executor = executor;
    }

    /**
     * 提交后台索引。状态会先同步进入 PROCESSING，因此上传响应不会长期停在
     * UPLOADING；线程池拒绝时立即写 FAILED，不会让文档永久卡住。
     */
    public void submit(Long documentId) {
        if (documentId == null) {
            return;
        }
        if (!markProcessing(documentId)) {
            log.debug("文档不满足索引条件，跳过提交: documentId={}", documentId);
            return;
        }
        try {
            executor.execute(() -> runIndex(documentId));
        } catch (RejectedExecutionException e) {
            log.warn("文档索引线程池已满，直接置为 FAILED: documentId={}", documentId);
            fail(documentId, "索引任务队列已满，请稍后重试");
        }
    }

    /**
     * 实际索引流程。包可见以便集成测试直接触发；生产路径统一走 {@link #submit(Long)}。
     */
    void runIndex(Long documentId) {
        Document document = documentMapper.selectById(documentId);
        if (document == null || document.getStatus() != DocumentStatus.PROCESSING) {
            log.warn("索引任务开始时文档已不存在或状态不是 PROCESSING: documentId={}", documentId);
            return;
        }

        try {
            byte[] content = storageService.read(document.getStorageUri());
            AgentParseResponse parsed = agentClient.parse(document, content);
            List<AgentParsedChunk> chunks = validateParsedChunks(parsed);

            List<DocumentChunk> saved = chunkService.replaceChunks(document, chunks);
            AgentIndexResponse indexed = agentClient.index(document, indexRequest(document, saved));
            Map<Long, String> refs = validateIndexResponse(saved, indexed);
            chunkService.updateVectorRefs(documentId, refs);

            complete(documentId, saved.size(), parsed.parserVersion(), indexed.embeddingModel());
            log.info("Document indexed: id={}, chunks={}, model={}, collection={}",
                    documentId, saved.size(), indexed.embeddingModel(), indexed.collection());
        } catch (Exception e) {
            log.error("Document indexing failed: id={}", documentId, e);
            fail(documentId, summarize(e));
        }
    }

    // ===================== 状态 =====================

    boolean markProcessing(Long documentId) {
        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, documentId)
                .isNull(Document::getDeletedAt)
                .in(Document::getStatus, DocumentStatus.UPLOADING, DocumentStatus.FAILED)
                .set(Document::getStatus, DocumentStatus.PROCESSING)
                .set(Document::getIndexError, null)
                .setSql("index_attempts = index_attempts + 1"));
        return updated == 1;
    }

    void complete(Long documentId, int chunkCount, String parserVersion, String embeddingModel) {
        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, documentId)
                .isNull(Document::getDeletedAt)
                .eq(Document::getStatus, DocumentStatus.PROCESSING)
                .set(Document::getStatus, DocumentStatus.READY)
                .set(Document::getParserVersion, abbreviate(parserVersion, 64))
                .set(Document::getChunkCount, chunkCount)
                .set(Document::getEmbeddingModel, abbreviate(embeddingModel, 128))
                .set(Document::getIndexedAt, LocalDateTime.now())
                .set(Document::getIndexError, null)
                .setSql("index_version = index_version + 1"));
        if (updated != 1) {
            throw new DocumentIndexException(409, "文档状态已变化，索引结果未标记为 READY");
        }
    }

    void fail(Long documentId, String error) {
        int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
                .eq(Document::getId, documentId)
                .isNull(Document::getDeletedAt)
                .eq(Document::getStatus, DocumentStatus.PROCESSING)
                .set(Document::getStatus, DocumentStatus.FAILED)
                .set(Document::getIndexError, abbreviate(error, MAX_INDEX_ERROR_LENGTH)));
        if (updated != 1) {
            log.warn("无法写入文档失败状态（可能已删除或状态已变化）: documentId={}, error={}",
                    documentId, abbreviate(error, 300));
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

    private AgentIndexRequest indexRequest(Document document, List<DocumentChunk> chunks) {
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
                payload);
    }

    private Map<Long, String> validateIndexResponse(List<DocumentChunk> saved, AgentIndexResponse indexed) {
        if (indexed == null || indexed.collection() == null || indexed.collection().isBlank()) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少 collection");
        }
        if (indexed.embeddingModel() == null || indexed.embeddingModel().isBlank()) {
            throw new DocumentIndexException(502, "Agent 索引响应缺少 embeddingModel");
        }
        if (indexed.embeddingModel().length() > 128) {
            throw new DocumentIndexException(502, "embeddingModel 超过 128 字符");
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
        return refs;
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
