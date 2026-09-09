package com.ails.backend.enums;

/**
 * 文档处理状态（document.status）
 */
public enum DocumentStatus {
    UPLOADING,
    PARSING,
    CHUNKING,
    INDEXING,
    READY,
    FAILED
}
