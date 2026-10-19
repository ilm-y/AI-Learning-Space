package com.ails.backend.enums;

/**
 * agent_run 的类型，对齐 {@code docs/database&API_design.md} §二十七。
 * <p>
 * 取值和数据库的 {@code ENUM} 一一对应，改名等于改 schema，两边必须一起动。
 */
public enum AgentRunType {

    /** 一次面向用户的对话生成（SSE 那一跳，由 ChatService 创建）。 */
    CHAT,

    /** 一次后台学习分析：从消息 / 批注里抽 Evidence 并投影 Understanding。 */
    ANALYSIS,

    /** 一次测试回答的判定。 */
    TEST,

    /** 一次 Memory 收敛。 */
    MEMORY
}
