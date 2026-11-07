package com.ails.backend.dto.conversation;

/**
 * 客户端读取到的来源引用。
 * <p>
 * 比持久化快照多一个 {@code available}：历史消息读取时按当前数据库状态重新判断
 * 文档 / chunk 是否仍然存在且属于当前用户。
 * <ul>
 *   <li>{@code available=true}：来源仍可打开；</li>
 *   <li>{@code available=false}：文档已删除、chunk 已被重新索引清理，或当前用户
 *       已无权访问。客户端必须把它渲染成「来源已不可用」，不能再当成有效链接。</li>
 * </ul>
 * 标题仍是引用发生时的快照，便于用户理解「当时引用的是什么」；它不构成新的
 * 文档访问权限，打开原文仍走按用户鉴权的文档接口。
 *
 * @param documentId 文档主键
 * @param chunkId    切片主键
 * @param chunkIndex 切片在文档内的序号，可空
 * @param sourceTitle 引用发生时的来源标题
 * @param available  当前是否仍可访问
 */
public record CitationResponse(
        Long documentId,
        Long chunkId,
        Integer chunkIndex,
        String sourceTitle,
        boolean available) {

    public static CitationResponse from(CitationMetadata metadata, boolean available) {
        if (metadata == null) {
            return null;
        }
        return new CitationResponse(
                metadata.documentId(),
                metadata.chunkId(),
                metadata.chunkIndex(),
                metadata.sourceTitle(),
                available);
    }
}
