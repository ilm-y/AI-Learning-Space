package com.ails.backend.service;

import com.ails.backend.dto.conversation.CitationMetadata;
import com.ails.backend.dto.conversation.CitationResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentChunkMapper;
import com.ails.backend.mapper.DocumentMapper;
import com.ails.backend.utils.JsonUtil;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 消息来源引用的持久化、版本快照与历史可用性判定。
 * <p>
 * <b>存什么：</b>只存 {@link CitationMetadata}（文档 / 切片身份 + 标题快照 +
 * 引用发生时的 chunk 版本），不存 chunk 正文。正文属于文档，重新索引后会变化；
 * 消息表保存一份副本只会让「引用的是哪一版资料」变成两份互相矛盾的事实。
 * <p>
 * <b>读取时判什么：</b>历史引用是否仍可用，不能靠写入时的布尔值 —— 文档可能在
 * 之后被删除或重新索引。这里在每次读取时批量检查：
 * <ol>
 *   <li>引用身份合法（非空、正数 ID、非空标题）；</li>
 *   <li>文档仍存在（MyBatis-Plus 的 {@code @TableLogic} 自动排除软删），且属于当前用户；</li>
 *   <li>文档状态为 {@link DocumentStatus#READY}；</li>
 *   <li>被引用的 chunk 仍存在，且确实属于该文档；</li>
 *   <li>chunk 的 {@code indexVersion} 等于文档当前活跃版本。</li>
 * </ol>
 * 任一不满足即 {@code available=false}。客户端据此显示「来源已不可用」，
 * 而不是给出一条会 404 的有效链接。
 * <p>
 * <b>历史标签：</b>若引用产生于更早的版本（优先用持久化快照，旧记录用现存 chunk
 * 版本兜底），返回 {@code isHistorical=true}。它只表达「当时引用过旧版本」，
 * 不代表旧 chunk 被保留；本实现不保留旧 chunk，因此不会与 RAG 清理策略冲突。
 * <p>
 * <b>权限边界：</b>引用永远随消息列表返回，而消息列表本身要求对话归属当前用户；
 * 本类不提供「按引用 id 直接读 chunk」的接口，因此不存在绕过文档权限的入口。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class CitationService {

    private final DocumentMapper documentMapper;
    private final DocumentChunkMapper chunkMapper;

    /** 把引用列表写成 JSON 列；空列表写 NULL，保持历史行的语义一致。 */
    public String serialize(List<CitationMetadata> citations) {
        if (citations == null || citations.isEmpty()) {
            return null;
        }
        return JsonUtil.toJson(citations);
    }

    /**
     * 解析 JSON 列。损坏的 JSON 只影响引用展示，不能让整条消息列表 500，
     * 因此按空列表处理并留下 warning。
     */
    public List<CitationMetadata> parse(String json) {
        if (json == null || json.isBlank()) {
            return List.of();
        }
        try {
            return JsonUtil.toList(json, CitationMetadata.class);
        } catch (RuntimeException e) {
            log.warn("消息引用 JSON 解析失败，按无引用处理: {}", e.getMessage());
            return List.of();
        }
    }

    /**
     * 为一批引用盖章「引用发生时的 chunk 版本」。写入前只查一次 chunk；
     * 查不到的引用保留原值（通常为 null），不伪造版本。
     * <p>
     * 该快照只服务历史标签，查询失败不能阻止回答落库；调用方负责降级。
     */
    public List<CitationMetadata> stampIndexVersions(List<CitationMetadata> citations) {
        if (citations == null || citations.isEmpty()) {
            return List.of();
        }
        Set<Long> chunkIds = new HashSet<>();
        for (CitationMetadata citation : citations) {
            if (citation != null && citation.chunkId() != null) {
                chunkIds.add(citation.chunkId());
            }
        }
        Map<Long, DocumentChunk> chunks = loadChunks(chunkIds);

        List<CitationMetadata> stamped = new ArrayList<>(citations.size());
        for (CitationMetadata citation : citations) {
            if (citation == null) {
                continue;
            }
            DocumentChunk chunk = citation.chunkId() == null
                    ? null : chunks.get(citation.chunkId());
            stamped.add(new CitationMetadata(
                    citation.documentId(),
                    citation.chunkId(),
                    citation.chunkIndex(),
                    citation.sourceTitle(),
                    chunk == null ? citation.indexVersion() : chunk.getIndexVersion()));
        }
        return List.copyOf(stamped);
    }

    /**
     * 批量计算一组引用的当前可用性与历史标签。文档 / chunk 各只查一次，
     * 与引用条数无关；实时 SSE 与历史读取共用这一条判定逻辑。
     */
    public List<CitationResponse> resolve(Long userId, List<CitationMetadata> citations) {
        if (citations == null || citations.isEmpty()) {
            return List.of();
        }
        Lookups lookups = loadLookups(userId, citations);
        return toResponses(userId, citations, lookups);
    }

    /**
     * 为一批消息批量计算引用可用性，返回 messageId → 引用列表。
     * 没有引用的消息不会出现在 map 里。
     */
    public Map<Long, List<CitationResponse>> resolveForMessages(
            Long userId, List<Message> messages) {
        Map<Long, List<CitationMetadata>> parsed = new LinkedHashMap<>();
        List<CitationMetadata> all = new ArrayList<>();
        for (Message message : messages) {
            List<CitationMetadata> citations = parse(message.getCitations());
            if (citations.isEmpty()) {
                continue;
            }
            parsed.put(message.getId(), citations);
            all.addAll(citations);
        }
        if (parsed.isEmpty()) {
            return Map.of();
        }

        // 所有消息的引用合并成一次批量查询，避免按消息条数产生 N+1。
        Lookups lookups = loadLookups(userId, all);

        Map<Long, List<CitationResponse>> result = new HashMap<>();
        for (Map.Entry<Long, List<CitationMetadata>> entry : parsed.entrySet()) {
            result.put(entry.getKey(), toResponses(userId, entry.getValue(), lookups));
        }
        return result;
    }

    /**
     * 单条引用的当前可用性；供测试与未来单条查询复用。
     * <p>
     * 判定条件：身份合法、文档存在且属于用户、文档 READY、chunk 存在且归属匹配、
     * chunk 版本等于文档活跃版本。{@code vectorRef} 不在这里重复校验 —— READY 文档的
     * 活跃版本由索引流程保证所有 chunk 都有向量引用，检索白名单也已经用
     * {@code chunk_count} 做过同一约束。
     */
    public boolean isAvailable(Long userId, CitationMetadata citation,
                               Map<Long, Document> documents,
                               Map<Long, DocumentChunk> chunks) {
        if (!hasValidIdentity(citation)) {
            return false;
        }
        Document document = documents.get(citation.documentId());
        if (document == null || userId == null || !userId.equals(document.getUserId())) {
            return false;
        }
        if (document.getStatus() != DocumentStatus.READY) {
            return false;
        }
        DocumentChunk chunk = chunks.get(citation.chunkId());
        if (chunk == null || !citation.documentId().equals(chunk.getDocumentId())) {
            return false;
        }
        return chunk.getIndexVersion() != null
                && chunk.getIndexVersion().equals(document.getIndexVersion());
    }

    /**
     * 单条引用是否产生于更早的索引版本。
     * <p>
     * 优先使用持久化的 {@code indexVersion} 快照；旧记录没有快照时，只有 chunk
     * 仍然存在才能用 chunk 版本兜底。文档已软删（查询结果里没有）或 chunk 已清理
     * 且无快照时返回 false —— 此时 API 无法区分「历史版本」与「已删除」。
     */
    public boolean isHistorical(Long userId, CitationMetadata citation,
                                Map<Long, Document> documents,
                                Map<Long, DocumentChunk> chunks) {
        if (citation == null || citation.documentId() == null
                || citation.documentId() <= 0) {
            return false;
        }
        Document document = documents.get(citation.documentId());
        if (document == null || userId == null || !userId.equals(document.getUserId())
                || document.getIndexVersion() == null) {
            return false;
        }

        Integer citedVersion = citation.indexVersion();
        if (citedVersion == null) {
            DocumentChunk chunk = citation.chunkId() == null
                    ? null : chunks.get(citation.chunkId());
            if (chunk == null || !citation.documentId().equals(chunk.getDocumentId())) {
                return false;
            }
            citedVersion = chunk.getIndexVersion();
        }
        return citedVersion != null && citedVersion < document.getIndexVersion();
    }

    private boolean hasValidIdentity(CitationMetadata citation) {
        return citation != null
                && citation.documentId() != null && citation.documentId() > 0
                && citation.chunkId() != null && citation.chunkId() > 0
                && citation.sourceTitle() != null && !citation.sourceTitle().isBlank();
    }

    private Lookups loadLookups(Long userId, Collection<CitationMetadata> citations) {
        Set<Long> documentIds = new HashSet<>();
        Set<Long> chunkIds = new HashSet<>();
        for (CitationMetadata citation : citations) {
            if (citation == null) {
                continue;
            }
            if (citation.documentId() != null) {
                documentIds.add(citation.documentId());
            }
            if (citation.chunkId() != null) {
                chunkIds.add(citation.chunkId());
            }
        }
        return new Lookups(loadDocuments(userId, documentIds), loadChunks(chunkIds));
    }

    private List<CitationResponse> toResponses(Long userId,
                                               List<CitationMetadata> citations,
                                               Lookups lookups) {
        List<CitationResponse> responses = new ArrayList<>(citations.size());
        for (CitationMetadata citation : citations) {
            if (citation == null) {
                continue;
            }
            responses.add(CitationResponse.from(citation,
                    isAvailable(userId, citation, lookups.documents(), lookups.chunks()),
                    isHistorical(userId, citation, lookups.documents(), lookups.chunks())));
        }
        return List.copyOf(responses);
    }

    private Map<Long, Document> loadDocuments(Long userId, Collection<Long> documentIds) {
        if (documentIds.isEmpty()) {
            return Map.of();
        }
        Map<Long, Document> result = new HashMap<>();
        // selectBatchIds 由 @TableLogic 自动附加 deleted_at IS NULL：软删文档不会出现。
        for (Document document : documentMapper.selectBatchIds(documentIds)) {
            // 权限在加载时就收紧，避免后续任何调用方忘记比对 owner。
            if (userId != null && userId.equals(document.getUserId())) {
                result.put(document.getId(), document);
            }
        }
        return result;
    }

    private Map<Long, DocumentChunk> loadChunks(Collection<Long> chunkIds) {
        if (chunkIds.isEmpty()) {
            return Map.of();
        }
        Map<Long, DocumentChunk> result = new HashMap<>();
        for (DocumentChunk chunk : chunkMapper.selectBatchIds(chunkIds)) {
            result.put(chunk.getId(), chunk);
        }
        return result;
    }

    private record Lookups(Map<Long, Document> documents,
                           Map<Long, DocumentChunk> chunks) {
    }
}
