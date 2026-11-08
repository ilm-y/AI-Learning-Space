package com.ails.backend.dto.conversation;

import com.ails.backend.dto.agent.AgentCitation;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * 一条持久化的来源引用快照（{@code message.citations} 的元素）。
 * <p>
 * 只存身份与展示标题，<b>不存 chunk 正文</b>：
 * <ul>
 *   <li>历史消息需要还原「当时引用了哪份资料的哪个片段」；</li>
 *   <li>正文会随重新索引变化，复制一份快照反而制造两份事实；</li>
 *   <li>是否仍可访问由读取时按当前数据库状态重新计算（见 {@link CitationResponse}）。</li>
 * </ul>
 * <p>
 * {@code indexVersion} 是引用发生时的 chunk 版本快照，仅用于历史标签：
 * 它不参与权限判断，也不保证旧 chunk 仍然存在。旧记录该字段为 {@code null}，
 * 读取时由 {@code CitationService} 在 chunk 仍存在的情况下用 chunk 版本兜底。
 * <p>
 * {@code @JsonIgnoreProperties(ignoreUnknown = true)} 让后续字段扩展不会再因为
 * 旧实例不认识新键而把整条引用列表解析失败（解析失败会降级为空列表）。
 *
 * @param documentId   文档主键
 * @param chunkId      切片主键
 * @param chunkIndex   切片在文档内的序号，可空
 * @param sourceTitle  引用发生时的来源标题快照
 * @param indexVersion 引用发生时的 chunk 索引版本，可空
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record CitationMetadata(
        Long documentId,
        Long chunkId,
        Integer chunkIndex,
        String sourceTitle,
        Integer indexVersion) {

    /** 兼容旧调用方/旧 JSON：没有版本快照时保持 null。 */
    public CitationMetadata(Long documentId, Long chunkId, Integer chunkIndex, String sourceTitle) {
        this(documentId, chunkId, chunkIndex, sourceTitle, null);
    }

    public static CitationMetadata fromAgent(AgentCitation citation) {
        if (citation == null) {
            return null;
        }
        return new CitationMetadata(
                citation.documentId(),
                citation.chunkId(),
                citation.chunkIndex(),
                citation.sourceTitle(),
                null);
    }
}
