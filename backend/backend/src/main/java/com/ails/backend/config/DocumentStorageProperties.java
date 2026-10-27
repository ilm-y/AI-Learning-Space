package com.ails.backend.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 文档原文存储配置（{@code ails.document.storage.*}）。
 *
 * @param root         存储根目录；相对路径基于后端进程工作目录
 * @param maxSizeBytes 单文件字节上限；小于等于 0 时使用默认值
 */
@ConfigurationProperties(prefix = "ails.document.storage")
public record DocumentStorageProperties(String root, long maxSizeBytes) {

    /** 开发环境默认存储目录（相对后端进程工作目录）。 */
    public static final String DEFAULT_ROOT = "data/documents";

    /** 开发环境默认单文件上限：10 MiB。 */
    public static final long DEFAULT_MAX_SIZE_BYTES = 10L * 1024 * 1024;
}
