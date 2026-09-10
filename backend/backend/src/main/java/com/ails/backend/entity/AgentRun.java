package com.ails.backend.entity;

import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Agent 执行轨迹（agent_run）。
 * <p>
 * 一次 SSE 对话请求对应一条 run_type = CHAT 的记录；一次异步学习分析对应一条
 * run_type = EXTRACTION 的记录（其 id 就是 Evidence 去重用的 agentRunId）。
 * <p>
 * 只保存结构化摘要，不保存 LLM 内部思维链。
 */
@Data
@TableName("agent_run")
public class AgentRun {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long conversationId;

    /** 对应的 learning_session */
    private Long sessionId;

    private AgentRunType runType;

    private AgentRunStatus status;

    private String inputSummary;

    private String outputSummary;

    private Integer durationMs;

    private String errorStack;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    private LocalDateTime finishedAt;
}
