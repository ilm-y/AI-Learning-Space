package com.ails.backend.service;

import com.ails.backend.dto.document.agent.AgentParsedChunk;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.mapper.DocumentChunkMapper;
import com.ails.backend.utils.JsonUtil;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * {@code document_chunk} 的写入边界。
 *
 * <p>所有替换操作在一个事务里完成：先删除该文档旧块，再写入新块。
 * 若任意一次 insert 失败（例如唯一键冲突），整个事务回滚，旧块仍在，
 * 不会出现「删了一半」或重复 chunk。重新索引因此天然幂等。</p>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class DocumentChunkService {

    private final DocumentChunkMapper chunkMapper;

    /**
     * 替换文档的全部 chunk，返回带 MySQL 自增 ID 的实体列表。
     */
    @Transactional
    public List<DocumentChunk> replaceChunks(Document document, List<AgentParsedChunk> chunks) {
        chunkMapper.delete(new LambdaQueryWrapper<DocumentChunk>()
                .eq(DocumentChunk::getDocumentId, document.getId()));

        List<DocumentChunk> saved = new ArrayList<>(chunks.size());
        for (AgentParsedChunk chunk : chunks) {
            DocumentChunk entity = new DocumentChunk();
            entity.setDocumentId(document.getId());
            entity.setChunkIndex(chunk.chunkIndex());
            entity.setContent(chunk.content());
            entity.setTokenCount(chunk.tokenCount());
            entity.setMetadataJson(metadataJson(chunk));
            int inserted = chunkMapper.insert(entity);
            if (inserted != 1 || entity.getId() == null) {
                throw new IllegalStateException("写入文档块失败: documentId="
                        + document.getId() + ", chunkIndex=" + chunk.chunkIndex());
            }
            saved.add(entity);
        }
        log.info("Replaced document chunks: documentId={}, count={}", document.getId(), saved.size());
        return saved;
    }

    /**
     * 将 Agent 返回的 Qdrant point ID 写回 chunk。按 document_id + chunk_id 双条件更新，
     * 防止错误覆盖其他文档的块。
     */
    @Transactional
    public void updateVectorRefs(Long documentId, Map<Long, String> refs) {
        for (Map.Entry<Long, String> entry : refs.entrySet()) {
            int updated = chunkMapper.update(null, new LambdaUpdateWrapper<DocumentChunk>()
                    .eq(DocumentChunk::getId, entry.getKey())
                    .eq(DocumentChunk::getDocumentId, documentId)
                    .set(DocumentChunk::getVectorRef, entry.getValue()));
            if (updated != 1) {
                throw new IllegalStateException("更新 chunk 向量引用失败: documentId="
                        + documentId + ", chunkId=" + entry.getKey());
            }
        }
    }

    public List<DocumentChunk> listByDocument(Long documentId) {
        return chunkMapper.selectList(new LambdaQueryWrapper<DocumentChunk>()
                .eq(DocumentChunk::getDocumentId, documentId)
                .orderByAsc(DocumentChunk::getChunkIndex));
    }

    private static String metadataJson(AgentParsedChunk chunk) {
        Map<String, Object> metadata = new LinkedHashMap<>();
        if (chunk.metadata() != null) {
            metadata.putAll(chunk.metadata());
        }
        metadata.put("tokenCountEstimated",
                Boolean.TRUE.equals(chunk.tokenCountEstimated()));
        return JsonUtil.toJson(metadata);
    }
}
