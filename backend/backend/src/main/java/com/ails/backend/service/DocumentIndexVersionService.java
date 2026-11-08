package com.ails.backend.service;

import com.ails.backend.dto.document.VersionCollection;
import com.ails.backend.entity.Document;
import com.ails.backend.mapper.DocumentIndexVersionMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 索引版本 → Qdrant collection 的持久记录与旧版本清理。
 *
 * <p><b>RAG-02 的核心不变量：</b>每一批 points 只能用它写入时的 collection 删除。
 * {@code document.vector_collection} 在切换活跃版本时会被覆盖，因此不能作为旧版本
 * 的定位依据。这里的 {@code document_index_version} 表按
 * {@code (document_id, index_version)} 记录真实 collection，清理按该记录分组。</p>
 *
 * <p><b>不可定位边界：</b>迁移前的非活跃版本、以及 {@code vector_collection} 为
 * NULL 的 legacy 文档没有记录。清理<b>不会猜测</b> collection 名：这些版本的
 * MySQL chunk 会被删除，但 Qdrant 中可能永久残留，日志会明确标记
 * {@code cleanup=unlocatable}，运维按 runbook 人工处理。</p>
 */
@Slf4j
@Service
public class DocumentIndexVersionService {

    private final DocumentIndexVersionMapper versionMapper;
    private final DocumentChunkService chunkService;
    private final AgentDocumentClient agentClient;

    public DocumentIndexVersionService(DocumentIndexVersionMapper versionMapper,
                                       DocumentChunkService chunkService,
                                       AgentDocumentClient agentClient) {
        this.versionMapper = versionMapper;
        this.chunkService = chunkService;
        this.agentClient = agentClient;
    }

    /** 记录某个索引版本实际写入的 collection。索引成功（切换前）调用。 */
    public void record(Long documentId, int indexVersion, String collection) {
        if (documentId == null || collection == null || collection.isBlank()) {
            throw new IllegalStateException("记录索引版本 collection 缺少必要参数: documentId="
                    + documentId + ", indexVersion=" + indexVersion);
        }
        versionMapper.upsert(documentId, indexVersion, collection);
    }

    /** 版本记录不存在时返回 {@code null}（不可定位，不是错误）。 */
    public String findCollection(Long documentId, int indexVersion) {
        return versionMapper.selectCollection(documentId, indexVersion);
    }

    public void deleteVersions(Long documentId, List<Integer> versions) {
        if (versions == null || versions.isEmpty()) {
            return;
        }
        versionMapper.deleteVersions(documentId, versions);
    }

    public void deleteByDocument(Long documentId) {
        versionMapper.deleteByDocument(documentId);
    }

    /**
     * 清理旧版本：按“该版本实际所在的 collection”分组删除 points，成功后删除
     * MySQL chunk 与版本记录。
     *
     * <p><b>失败语义：</b>某个 collection 删除失败时，该组 chunk/版本记录全部保留，
     * 下一次定时清理还能重新发现并重试；其他已经成功的组照常清理。无论是否抛出，
     * 都不会提前删除失败组的定位信息。</p>
     *
     * @return 本次已经完成（向量已删或确认不可定位）的版本号
     */
    public List<Integer> purgeStaleVersions(Document document, List<Integer> staleVersions) {
        if (staleVersions == null || staleVersions.isEmpty()) {
            return List.of();
        }
        Long documentId = document.getId();
        Map<Integer, String> collectionByVersion = new LinkedHashMap<>();
        for (VersionCollection row : versionMapper.selectByVersions(documentId, staleVersions)) {
            if (row.indexVersion() != null
                    && row.vectorCollection() != null
                    && !row.vectorCollection().isBlank()) {
                collectionByVersion.put(row.indexVersion(), row.vectorCollection());
            }
        }

        Map<String, List<Integer>> byCollection = new TreeMap<>();
        List<Integer> unlocatable = new ArrayList<>();
        for (Integer version : staleVersions) {
            String collection = version == null ? null : collectionByVersion.get(version);
            if (collection == null) {
                if (version != null) {
                    unlocatable.add(version);
                }
            } else {
                byCollection.computeIfAbsent(collection, key -> new ArrayList<>()).add(version);
            }
        }

        List<Integer> completed = new ArrayList<>();
        RuntimeException failure = null;
        for (Map.Entry<String, List<Integer>> entry : byCollection.entrySet()) {
            List<Integer> versions = entry.getValue();
            try {
                // 只有版本 0 的记录才代表迁移前 legacy points（没有 index_version 字段）。
                agentClient.deleteVersions(document, versions, versions.contains(0), entry.getKey());
                completed.addAll(versions);
            } catch (RuntimeException e) {
                failure = failure == null ? e : failure;
                log.warn("旧版本向量清理失败，保留定位信息等待重试: documentId={}, "
                                + "collection={}, versions={}, error={}",
                        documentId, entry.getKey(), versions, e.toString());
            }
        }

        if (!unlocatable.isEmpty()) {
            // 不猜测 collection：比“猜错 collection 却删掉 MySQL 记录”更安全。
            log.warn("无法定位旧版本 collection，Qdrant points 可能永久残留: "
                            + "cleanup=unlocatable, documentId={}, activeVersion={}, versions={}, "
                            + "collection=<unlocatable>",
                    documentId, document.getIndexVersion(), unlocatable);
            completed.addAll(unlocatable);
        }

        if (!completed.isEmpty()) {
            // 先删 chunk 再删版本记录；两者都只是“定位信息”，points 已处理或确认无记录。
            chunkService.deleteVersions(documentId, completed);
            versionMapper.deleteVersions(documentId, completed);
        }

        if (failure != null) {
            throw failure;
        }
        return completed;
    }
}
