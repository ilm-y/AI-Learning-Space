package com.ails.backend.config;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.ThreadPoolExecutor;

/**
 * 文档索引专用线程池。
 *
 * <p>刻意不复用 {@code chatStreamExecutor}：索引是外部 HTTP + Embedding + Qdrant
 * 的 I/O 长任务，占住 SSE 线程会直接拖慢用户可见的对话。这里使用有界队列和
 * AbortPolicy —— 队列满时把拒绝信号抛回调用方，由业务层显式落 FAILED。</p>
 */
@Configuration
@EnableConfigurationProperties(DocumentIndexProperties.class)
@EnableScheduling
public class DocumentIndexConfig {

    @Bean("documentIndexExecutor")
    public TaskExecutor documentIndexExecutor(DocumentIndexProperties properties) {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(properties.corePoolSize());
        executor.setMaxPoolSize(properties.maxPoolSize());
        executor.setQueueCapacity(properties.queueCapacity());
        executor.setThreadNamePrefix("document-index-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.AbortPolicy());
        executor.setWaitForTasksToCompleteOnShutdown(true);
        executor.setAwaitTerminationSeconds(15);
        executor.initialize();
        return executor;
    }
}
