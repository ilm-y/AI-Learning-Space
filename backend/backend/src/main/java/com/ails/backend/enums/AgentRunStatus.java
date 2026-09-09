package com.ails.backend.enums;

/**
 * Agent 执行状态（agent_run.status）
 */
public enum AgentRunStatus {
    STARTED,
    THINKING,
    TOOL_CALLING,
    FINISHED,
    TIMEOUT,
    ERROR
}
