package com.ails.backend.mq;

/**
 * 分析任务投递失败，并说明这次失败是否「确定」。
 * <p>
 * 发布确认是异步的：{@code convertAndSend} 返回不等于 broker 收下了消息。这里把失败分成两类，
 * 因为调用方对它们的处理完全不同：
 * <ul>
 *   <li>{@link Outcome#DEFINITE_FAILURE}：NACK、mandatory return、连接根本没建立。
 *       消息不会到达 Worker，可以安全地退回同步分析；</li>
 *   <li>{@link Outcome#UNKNOWN}：等确认超时、等待期间连接断开/线程中断。
 *       消息可能已经在队列里，甚至已经被 Worker 处理。此时再跑一次同步分析，
 *       同一个任务就会调用两次模型。只能保留 PENDING 并记录诊断信息，等结果自然到达。</li>
 * </ul>
 * 没有 Transactional Outbox 时，「确认超时」永远存在这段未知窗口；本类的作用是不要把
 * 未知窗口伪装成确定失败。
 */
public class AnalysisPublishException extends RuntimeException {

    public enum Outcome {
        DEFINITE_FAILURE,
        UNKNOWN
    }

    private final Outcome outcome;

    public AnalysisPublishException(Outcome outcome, String message, Throwable cause) {
        super(message, cause);
        this.outcome = outcome;
    }

    public Outcome outcome() {
        return outcome;
    }

    public static AnalysisPublishException definite(String message, Throwable cause) {
        return new AnalysisPublishException(Outcome.DEFINITE_FAILURE, message, cause);
    }

    public static AnalysisPublishException unknown(String message, Throwable cause) {
        return new AnalysisPublishException(Outcome.UNKNOWN, message, cause);
    }
}
