package com.ails.backend.enums;

/**
 * Agent 执行类型（agent_run.run_type）。
 * <p>
 * 与 {@link AgentRunStatus} 正交：run_type 说明「在做什么」，status 说明「做得怎么样」。
 */
public enum AgentRunType {
    /** 一轮对话（对应一次 SSE 请求） */
    CHAT,
    /** 从 Message / Highlight / TestAnswer 抽取 Evidence */
    EXTRACTION,
    /** 更新 Understanding 并产生 Revision */
    UPDATE,
    /** 生成 / 评价测试 */
    TEST,
    /** Memory 调和 */
    MEMORY
}
