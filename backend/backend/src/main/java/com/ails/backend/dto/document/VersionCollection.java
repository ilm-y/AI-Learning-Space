package com.ails.backend.dto.document;

/**
 * 某个索引版本实际写入的 Qdrant collection。
 *
 * <p>用于 RAG-02 跨 collection 清理：清理必须按“该版本自己的 collection”删除 points，
 * 而不是用本次新索引的 collection 或当前活跃 collection。</p>
 */
public record VersionCollection(Integer indexVersion, String vectorCollection) {
}
