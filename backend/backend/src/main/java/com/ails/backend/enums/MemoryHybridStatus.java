package com.ails.backend.enums;

/**
 * 混合检索的降级状态。
 *
 * <p>调用方必须把它当作检索结果的一部分呈现：把「检索不可用」伪装成「没有相关记忆」
 * 会让故障无法被发现。</p>
 */
public enum MemoryHybridStatus {
    /** 词法路与向量路都成功。 */
    OK,
    /** 只有词法路成功（Agent/Qdrant 不可用或超时）。 */
    DEGRADED_VECTOR,
    /** 只有向量路成功（FULLTEXT 索引缺失或查询失败）。 */
    DEGRADED_KEYWORD,
    /** 两路都失败，或回源复核失败；不能返回未复核数据。 */
    UNAVAILABLE,
    /** 查询为空，按 importance/updatedAt 返回 MySQL 列表，不做融合。 */
    BLANK_QUERY
}
