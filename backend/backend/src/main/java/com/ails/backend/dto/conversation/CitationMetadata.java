package com.ails.backend.dto.conversation;

import com.ails.backend.dto.agent.AgentCitation;

/**
 * 一条持久化的来源引用快照（{@code message.citations} 的元素）。
 * <p>
 * 只存身份与展示标题，<b>不存 chunk 正文</b>：
 * <ul>
 *   <li>历史消息需要还原「当时引用了哪份资料的哪个片段」；</li>
 *   <li>正文会随重新索引变化，复制一份快照反而制造两份事实；</li>
 *   <li>是否仍可访问由读取时按当前数据库状态重新计算（见 {@link CitationResponse}）。</li>
 * </ul>
 *
 * @param documentId 文档主键
 * @param chunkId    切片主键
 * @param chunkIndex 切片在文档内的序号，可空
 * @param sourceTitle 引用发生时的来源标题快照
 */
public record CitationMetadata(
        Long documentId,
        Long chunkId,
        Integer chunkIndex,
        String sourceTitle) {

    public static CitationMetadata fromAgent(AgentCitation citation) {
        if (citation == null) {
            return null;
        }
        return new CitationMetadata(
                citation.documentId(),
                citation.chunkId(),
                citation.chunkIndex(),
                citation.sourceTitle());
    }
}
