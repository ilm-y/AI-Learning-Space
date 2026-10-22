package com.ails.backend.mq;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.mapper.AgentRunMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;

/**
 * 分析任务的状态兜底：把长时间停在 PENDING / RUNNING 的 {@code agent_run} 标记为 STALE。
 * <p>
 * <b>为什么需要它。</b> 投递与建任务不是原子的，而且至少有四条路径会让任务没有结果：
 * <ol>
 *   <li>INSERT PENDING 之后、消息投递之前进程崩溃；</li>
 *   <li>消息进入 result 队列但反序列化失败，容器直接拒绝进 DLQ（监听器根本没进来）；</li>
 *   <li>Worker 永久下线，连 FAILED 结果都发不出来；</li>
 *   <li>结果重试耗尽时数据库仍不可用，{@code markFailedPermanently} 也失败。</li>
 * </ol>
 * 没有兜底时这些任务会永远停在 PENDING，前端与运维都无法区分「还在排队」和「已经丢了」。
 *
 * <h3>为什么是 STALE 而不是 FAILED</h3>
 * {@code created_at} 超阈值只能说明「还没看到终态」，不能证明任务没有执行。任务可能仍在
 * 队列里，甚至正在调用模型。直接置 FAILED 会让随后到达的合法结果被当成 DUPLICATE 丢弃。
 * 因此这里只写一个可观测的 STALE 标记；结果消费者仍允许 STALE → RUNNING，晚到的结果可以正常收敛。
 *
 * <h3>安全性</h3>
 * <ul>
 *   <li>只处理 {@code run_type = ANALYSIS}；</li>
 *   <li>只处理 {@code created_at} 早于阈值的行，不碰刚创建的任务；</li>
 *   <li>UPDATE 带 {@code status IN ('PENDING','RUNNING')} 条件，与结果消费者的抢占是
 *       同一条行锁上的竞争：谁先提交谁赢。补偿扫描赢时结果消息之后仍能抢占 STALE；
 *       结果消费者赢时这里更新 0 行，不会覆盖真正的终态；</li>
 *   <li><b>不自动重投。</b> {@code agent_run} 只保存 input_summary / trace_id / 关联 ID，
 *       没有完整的任务载荷（历史、会话、助手回复），无法据此重建请求消息。这里只标记状态，
 *       消息本身留在队列/DLQ 里按原逻辑处理，或由人工按 DLQ 原始消息恢复。</li>
 * </ul>
 *
 * <h3>代价</h3>
 * STALE 只是「可能遗留」的队列，不是终态；真正的遗留任务需要人工确认。这样做牺牲了
 * 「自动收敛到 FAILED」的便利，换取「不误杀仍在执行的任务」。
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ails.analysis.mode", havingValue = "mq")
public class AnalysisStaleRunCompensator {

    private static final int ERROR_MAX_LENGTH = 1000;

    private final AgentRunMapper agentRunMapper;
    private final AnalysisCompensationProperties properties;

    @Scheduled(fixedDelayString = "${ails.analysis.compensation.interval-ms:600000}")
    public void compensate() {
        if (!properties.enabled()) {
            return;
        }
        LocalDateTime cutoff = LocalDateTime.now().minus(Duration.ofMillis(properties.staleAfterMs()));
        List<AgentRun> stale = agentRunMapper.selectStaleAnalysisRuns(cutoff, properties.batchSize());
        if (stale.isEmpty()) {
            return;
        }
        String error = truncate("补偿扫描: 分析任务超过 "
                + Duration.ofMillis(properties.staleAfterMs()).toMinutes()
                + " 分钟未完成，标记为 STALE（任务可能仍在队列/执行中；若结果到达仍会正常收敛）",
                ERROR_MAX_LENGTH);
        int marked = 0;
        for (AgentRun run : stale) {
            try {
                if (agentRunMapper.markAnalysisRunStale(run.getId(), cutoff, error) > 0) {
                    marked++;
                    log.warn("分析任务超时未完成，标记 STALE, taskId={}, traceId={}, createdAt={}",
                            run.getId(), run.getTraceId(), run.getCreatedAt());
                }
            } catch (RuntimeException e) {
                // 单条失败不影响本批其余任务；下一轮还会扫到它。
                log.error("补偿扫描更新任务失败, taskId={}", run.getId(), e);
            }
        }
        log.warn("分析任务补偿扫描完成: 命中 {} 条, 实际标记 STALE {} 条, cutoff={}",
                stale.size(), marked, cutoff);
    }

    private static String truncate(String text, int max) {
        return text.length() <= max ? text : text.substring(0, max);
    }
}
