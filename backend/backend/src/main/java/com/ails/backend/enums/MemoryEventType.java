package com.ails.backend.enums;

/**
 * Memory 向量索引事件类型。
 *
 * <p>只覆盖「可检索状态发生变化」的两类事件：</p>
 * <ul>
 *   <li>{@link #MEMORY_UPSERT}：新建 ACTIVE，或 ACTIVE 的内容/作用域发生变化；</li>
 *   <li>{@link #MEMORY_DELETE}：ACTIVE → ARCHIVED，需要删除 Qdrant 中的点。</li>
 * </ul>
 *
 * <p>CANDIDATE 不进入索引，因此候选的创建与更新都不会产生事件（见设计文档 D3）。</p>
 */
public enum MemoryEventType {
    MEMORY_UPSERT,
    MEMORY_DELETE
}
