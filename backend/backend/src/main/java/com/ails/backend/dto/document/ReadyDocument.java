package com.ails.backend.dto.document;

import lombok.Data;

/**
 * Agent 知识检索白名单的一项：文档 ID + 当前活跃索引版本。
 *
 * <p>Agent 需要版本号才能只检索“当前活跃版本”的 Qdrant points；
 * 只返回 ID 会让重建期间的新旧版本同时被召回。</p>
 */
@Data
public class ReadyDocument {

    private Long id;

    private Integer indexVersion;
}
