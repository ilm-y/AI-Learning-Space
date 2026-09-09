package com.ails.backend.entity;

import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Agent 执行轨迹表（agent_run，调试/可观测）
 */
@Data
@TableName("agent_run")
public class AgentRun {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    /** 可空 */
    private Long conversationId;

    /** 可空 */
    private Long learningSessionId;

    private AgentType agentType;

    private AgentRunStatus status;

    /** 输入截断 */
    private String inputSummary;

    /** 输出截断 */
    private String outputSummary;

    /** 耗时毫秒 */
    private Integer durationMs;

    /** 异常堆栈（截断） */
    private String errorStack;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    private LocalDateTime finishedAt;
}
