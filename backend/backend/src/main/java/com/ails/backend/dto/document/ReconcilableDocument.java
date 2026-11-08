package com.ails.backend.dto.document;

/**
 * 反向对账的候选文档（只读扫描结果）。
 *
 * <p>只包含对账需要的字段；是否真正对账还要由服务层结合版本表与 MySQL chunk 数判断。</p>
 */
public record ReconcilableDocument(
        Long id,
        Long userId,
        Long spaceId,
        Integer indexVersion,
        String vectorCollection) {
}
