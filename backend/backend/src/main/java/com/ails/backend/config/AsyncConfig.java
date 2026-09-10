package com.ails.backend.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

/**
 * 流式对话用的线程池。
 * <p>
 * SSE 不能占着 Tomcat 的请求线程跑完整轮生成，否则并发几下就把容器线程池吃干了，
 * 所以 agent 调用放在这个独立线程池里。
 */
@Configuration
public class AsyncConfig {

    @Bean("chatStreamExecutor")
    public TaskExecutor chatStreamExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(4);
        executor.setMaxPoolSize(16);
        executor.setQueueCapacity(64);
        executor.setThreadNamePrefix("chat-stream-");
        executor.setWaitForTasksToCompleteOnShutdown(true);
        executor.setAwaitTerminationSeconds(10);
        executor.initialize();
        return executor;
    }
}
