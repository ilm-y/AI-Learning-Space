package com.ails.backend.dto.conversation;

/**
 * 客户端读取到的来源引用。
 * <p>
 * 比持久化快照多一个 {@code available}：历史消息读取时按当前数据库状态重新判断
 * 文档 / chunk 是否仍然存在且属于当前用户。
 * <ul>
 *   <li>{@code available=true}：文档当前 READY，且被引用的 chunk 属于当前活跃
 *       {@code indexVersion}；</li>
 *   <li>{@code available=false, isHistorical=true}：引用产生于旧索引版本，当前已不是
 *       活跃版本；来源曾真实存在，但现在不能再当作当前内容打开；</li>
 *   <li>{@code available=false, isHistorical=false}：文档已删除、chunk 已清理，或
 *       无法判断版本。客户端渲染为「来源已删除/不可用」。</li>
 * </ul>
 * 标题仍是引用发生时的快照，便于用户理解「当时引用的是什么」；它不构成新的
 * 文档访问权限，打开原文仍走按用户鉴权的文档接口。
 *
 * @param documentId   文档主键
 * @param chunkId      切片主键
 * @param chunkIndex   切片在文档内的序号，可空
 * @param sourceTitle  引用发生时的来源标题
 * @param available    当前是否仍属于活跃 READY 版本
 * @param isHistorical 是否产生于更早的索引版本（快照或现存 chunk 版本可判断时）
 */
public record CitationResponse(
        Long documentId,
        Long chunkId,
        Integer chunkIndex,
        String sourceTitle,
        boolean available,
        boolean isHistorical) {

    public static CitationResponse from(CitationMetadata metadata, boolean available,
                                        boolean isHistorical) {
        if (metadata == null) {
            return null;
        }
        return new CitationResponse(
                metadata.documentId(),
                metadata.chunkId(),
                metadata.chunkIndex(),
                metadata.sourceTitle(),
                available,
                isHistorical);
    }

    /** 兼容旧调用方：没有历史快照时 isHistorical=false。 */
    public static CitationResponse from(CitationMetadata metadata, boolean available) {
        return from(metadata, available, false);
    }
}
