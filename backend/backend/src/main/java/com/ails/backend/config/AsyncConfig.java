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
 * <p>
 * <b>这里的容量不能按「常见线程池」的直觉配：</b>{@link ThreadPoolTaskExecutor} 是
 * <b>先排队、再扩容</b>（队列没满就不加线程），所以实际并发上限是
 * {@code corePoolSize}，不是 {@code maxPoolSize} —— 只有队列（128）被塞满之后才会去用
 * 超出的 48 个线程。而本池的任务是<b>长任务</b>（一轮生成几秒到几十秒，最坏挂满整个
 * SSE 超时），跟「短任务快速周转」的假设正好相反：只要 core 给 4，第 5 个用户就得在后边
 * 干等，前 4 个里任何一个卡住就一起卡。所以 core 直接给足，让并发上限是真的并发上限。
 * <p>
 * 队列给 128 是「缓冲」而不是「并发」：排队中的请求还没开始生成，用户至少能看到
 * {@code message.start}。队列 + 线程都满时按 AbortPolicy 抛 RejectedExecutionException，
 * 由 {@code ChatService.stream} 接住并回一个 503 的 {@code message.error} ——
 * 拒绝是显式的失败信号，比无声排队到 SSE 超时好得多。
 */
@Configuration
public class AsyncConfig {

    @Bean("chatStreamExecutor")
    public TaskExecutor chatStreamExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        // 真实并发上限，见类注释：长任务不适用「小 core + 大队列」
        executor.setCorePoolSize(16);
        executor.setMaxPoolSize(64);
        // 排队缓冲；满了就拒绝，不无限堆着等超时
        executor.setQueueCapacity(128);
        executor.setThreadNamePrefix("chat-stream-");
        // 优雅停机：让已经开始的生成跑完（最多等 10s），不然重启会把正在生成的回复整段丢掉
        executor.setWaitForTasksToCompleteOnShutdown(true);
        executor.setAwaitTerminationSeconds(10);
        executor.initialize();
        return executor;
    }
}
