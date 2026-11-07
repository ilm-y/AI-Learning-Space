package com.ails.backend.config;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.ThreadPoolExecutor;

/**
 * 混合检索线程池与 Memory 相关配置绑定。
 *
 * <p>刻意不复用 {@code chatStreamExecutor} / {@code documentIndexExecutor}：
 * 检索是用户请求路径上的短任务（预算 2.5s），必须与长任务隔离；线程池有界 +
 * {@link ThreadPoolExecutor.AbortPolicy}，拒绝时由 {@code HybridSearchService}
 * 捕获并降级为单路，不能让 {@code RejectedExecutionException} 冒到请求线程。</p>
 */
@Configuration
@EnableConfigurationProperties({MemorySearchProperties.class, MemoryOutboxProperties.class})
@EnableScheduling
public class MemorySearchConfig {

    @Bean("memorySearchExecutor")
    public TaskExecutor memorySearchExecutor(MemorySearchProperties properties) {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(properties.executorCoreSize());
        executor.setMaxPoolSize(properties.executorMaxSize());
        executor.setQueueCapacity(properties.executorQueueCapacity());
        executor.setThreadNamePrefix("memory-search-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.AbortPolicy());
        executor.setWaitForTasksToCompleteOnShutdown(true);
        executor.setAwaitTerminationSeconds(5);
        executor.initialize();
        return executor;
    }
}
