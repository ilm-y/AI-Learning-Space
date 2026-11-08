package com.ails.backend.config;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;

/**
 * 启用 {@link RetrievalProperties} 的构造器绑定。
 *
 * <p>单独成类而不是挂在 {@code DocumentStorageConfig} 上：检索策略与文件存储是两条
 * 独立的配置面，拆开后测试上下文可以只启用需要的配置。</p>
 */
@Configuration
@EnableConfigurationProperties(RetrievalProperties.class)
public class RetrievalConfig {
}
