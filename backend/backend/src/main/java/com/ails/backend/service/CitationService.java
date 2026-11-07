package com.ails.backend.service;

import com.ails.backend.dto.conversation.CitationMetadata;
import com.ails.backend.dto.conversation.CitationResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.entity.Message;
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
 * 消息来源引用的持久化与历史可用性判定。
 * <p>
 * <b>存什么：</b>只存 {@link CitationMetadata}（文档 / 切片身份 + 标题快照），
 * 不存 chunk 正文。正文属于文档，重新索引后会变化；消息表保存一份副本只会让
 * 「引用的是哪一版资料」变成两份互相矛盾的事实。
 * <p>
 * <b>读取时判什么：</b>历史引用是否仍可用，不能靠写入时的布尔值 —— 文档可能在
 * 之后被删除或重新索引。这里在每次读取时批量检查：
 * <ol>
 *   <li>文档仍存在（MyBatis-Plus 的 {@code @TableLogic} 自动排除软删）；</li>
 *   <li>文档属于当前用户（纵深防御，正常链路由检索白名单保证）；</li>
 *   <li>被引用的 chunk 仍存在，且确实属于该文档。</li>
 * </ol>
 * 任一不满足即 {@code available=false}。客户端据此显示「来源已不可用」，
 * 而不是给出一条会 404 的有效链接。
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
     * 为一批消息批量计算引用可用性，返回 messageId → 引用列表。
     * 没有引用的消息不会出现在 map 里。
     */
    public Map<Long, List<CitationResponse>> resolveForMessages(
            Long userId, List<Message> messages) {
        Map<Long, List<CitationMetadata>> parsed = new LinkedHashMap<>();
        Set<Long> documentIds = new HashSet<>();
        Set<Long> chunkIds = new HashSet<>();
        for (Message message : messages) {
            List<CitationMetadata> citations = parse(message.getCitations());
            if (citations.isEmpty()) {
                continue;
            }
            parsed.put(message.getId(), citations);
            for (CitationMetadata citation : citations) {
                if (citation.documentId() != null) {
                    documentIds.add(citation.documentId());
                }
                if (citation.chunkId() != null) {
                    chunkIds.add(citation.chunkId());
                }
            }
        }
        if (parsed.isEmpty()) {
            return Map.of();
        }

        Map<Long, Document> documents = loadDocuments(userId, documentIds);
        Map<Long, DocumentChunk> chunks = loadChunks(chunkIds);

        Map<Long, List<CitationResponse>> result = new HashMap<>();
        for (Map.Entry<Long, List<CitationMetadata>> entry : parsed.entrySet()) {
            List<CitationResponse> responses = new ArrayList<>(entry.getValue().size());
            for (CitationMetadata citation : entry.getValue()) {
                responses.add(CitationResponse.from(citation,
                        isAvailable(userId, citation, documents, chunks)));
            }
            result.put(entry.getKey(), List.copyOf(responses));
        }
        return result;
    }

    /** 单条引用的可用性；供测试与未来单条查询复用。 */
    public boolean isAvailable(Long userId, CitationMetadata citation,
                               Map<Long, Document> documents,
                               Map<Long, DocumentChunk> chunks) {
        if (citation == null || citation.documentId() == null || citation.chunkId() == null) {
            return false;
        }
        Document document = documents.get(citation.documentId());
        if (document == null || userId == null || !userId.equals(document.getUserId())) {
            return false;
        }
        DocumentChunk chunk = chunks.get(citation.chunkId());
        return chunk != null && citation.documentId().equals(chunk.getDocumentId());
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
}
