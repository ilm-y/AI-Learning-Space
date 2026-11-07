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
 * <p><b>分阶段写入：</b>新构建的 chunk 写入新的 {@code index_version}，不触碰旧版本。
 * 只有 {@code document.index_version} 指向的版本才是活跃版本；切换由
 * {@link DocumentIndexService} 用一条条件 UPDATE 完成。</p>
 *
 * <p><b>为什么不先删旧块：</b>旧实现先 DELETE 再 INSERT，Qdrant 写入失败后旧块
 * 已经不可恢复。分区写入后，失败只需清理“本次 attempt”的块，旧版本原样保留。</p>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class DocumentChunkService {

    private final DocumentChunkMapper chunkMapper;

    /**
     * 写入某个构建版本的 chunk，返回带 MySQL 自增 ID 的实体列表。
     *
     * <p>先删除同一 {@code indexVersion} 的残留（正常为空），再整批插入；
     * 同一事务保证不会出现“写了一半”。租约（attempt）保证同一文档同时只有一个
     * writer，因此不会与其它构建交叉。</p>
     */
    @Transactional
    public List<DocumentChunk> stageChunks(Document document,
                                           List<AgentParsedChunk> chunks,
                                           int indexVersion) {
        chunkMapper.deleteByVersion(document.getId(), indexVersion);

        List<DocumentChunk> saved = new ArrayList<>(chunks.size());
        for (AgentParsedChunk chunk : chunks) {
            DocumentChunk entity = new DocumentChunk();
            entity.setDocumentId(document.getId());
            entity.setIndexVersion(indexVersion);
            entity.setChunkIndex(chunk.chunkIndex());
            entity.setContent(chunk.content());
            entity.setTokenCount(chunk.tokenCount());
            entity.setMetadataJson(metadataJson(chunk));
            int inserted = chunkMapper.insert(entity);
            if (inserted != 1 || entity.getId() == null) {
                throw new IllegalStateException("写入文档块失败: documentId="
                        + document.getId() + ", version=" + indexVersion
                        + ", chunkIndex=" + chunk.chunkIndex());
            }
            saved.add(entity);
        }
        log.info("Staged document chunks: documentId={}, version={}, count={}",
                document.getId(), indexVersion, saved.size());
        return saved;
    }

    /**
     * 将 Agent 返回的 Qdrant point ID 写回本次构建版本的 chunk。
     * 按 document_id + index_version + chunk_id 三重条件更新，防止错误覆盖其他版本。
     */
    @Transactional
    public void updateVectorRefs(Long documentId, int indexVersion, Map<Long, String> refs) {
        for (Map.Entry<Long, String> entry : refs.entrySet()) {
            int updated = chunkMapper.update(null, new LambdaUpdateWrapper<DocumentChunk>()
                    .eq(DocumentChunk::getId, entry.getKey())
                    .eq(DocumentChunk::getDocumentId, documentId)
                    .eq(DocumentChunk::getIndexVersion, indexVersion)
                    .set(DocumentChunk::getVectorRef, entry.getValue()));
            if (updated != 1) {
                throw new IllegalStateException("更新 chunk 向量引用失败: documentId="
                        + documentId + ", version=" + indexVersion
                        + ", chunkId=" + entry.getKey());
            }
        }
    }

    /** 全部版本，按版本、序号排序；用于测试/排查。 */
    public List<DocumentChunk> listByDocument(Long documentId) {
        return chunkMapper.selectList(new LambdaQueryWrapper<DocumentChunk>()
                .eq(DocumentChunk::getDocumentId, documentId)
                .orderByAsc(DocumentChunk::getIndexVersion)
                .orderByAsc(DocumentChunk::getChunkIndex));
    }

    /** 当前活跃版本。 */
    public List<DocumentChunk> listActiveByDocument(Long documentId, int activeVersion) {
        return chunkMapper.selectList(new LambdaQueryWrapper<DocumentChunk>()
                .eq(DocumentChunk::getDocumentId, documentId)
                .eq(DocumentChunk::getIndexVersion, activeVersion)
                .orderByAsc(DocumentChunk::getChunkIndex));
    }

    public int deleteVersion(Long documentId, int indexVersion) {
        return chunkMapper.deleteByVersion(documentId, indexVersion);
    }

    /** 精确删除指定版本；空列表是 no-op。 */
    public int deleteVersions(Long documentId, List<Integer> versions) {
        if (versions == null || versions.isEmpty()) {
            return 0;
        }
        return chunkMapper.deleteVersions(documentId, versions);
    }

    public int deleteOtherVersions(Long documentId, int activeVersion) {
        return chunkMapper.deleteOtherVersions(documentId, activeVersion);
    }

    public int deleteAll(Long documentId) {
        return chunkMapper.deleteAll(documentId);
    }

    public List<Integer> listOtherVersions(Long documentId, int activeVersion) {
        return chunkMapper.selectOtherVersions(documentId, activeVersion);
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
