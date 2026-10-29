package com.ails.backend.enums;

/**
 * Agent 执行状态（agent_run.status）
 * <p>
 * 对话 run 走 {@code RUNNING → COMPLETED / TRUNCATED / FAILED}；
 * 异步分析 run 走 {@code PENDING → RUNNING → SUCCEEDED / FAILED}：
 * <ul>
 *   <li>{@code PENDING} —— 任务已建、请求消息已投递，等待 Agent Worker 消费；</li>
 *   <li>{@code STALE} —— 超过阈值仍未完成，<b>只是「可能遗留」的可观测标记</b>，不是确认失败。
 *       任务可能仍在队列中或正在分析，晚到的结果仍允许把它推进到 RUNNING；</li>
 *   <li>{@code RUNNING} —— 结果消息抵达后，由结果消费者抢占处理权；</li>
 *   <li>{@code SUCCEEDED} —— 结果已校验并写入数据库；</li>
 *   <li>{@code FAILED} —— 分析失败、投递失败或重试耗尽。</li>
 * </ul>
 * {@code COMPLETED} 保留给同步对话路径，语义是「这次生成正常结束」；
 * {@code TRUNCATED} 表示生成因 Token 上限提前停止，内容不完整，不能当作正常完成。
 * <p>
 * 把「可能遗留」和「确认失败」分成两个状态，是为了避免补偿扫描把仍在执行的任务判死，
 * 从而让随后到达的合法结果被当作 DUPLICATE 丢弃。
 */
public enum AgentRunStatus {
    PENDING,
    STALE,
    RUNNING,
    COMPLETED,
    /** 回答因模型 Token 上限被截断：部分内容可展示，但不是完整回答。 */
    TRUNCATED,
    SUCCEEDED,
    FAILED
}
