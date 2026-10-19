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
 * run_type = ANALYSIS 的记录。分析 run 的 id 会记进 Evidence 的 first/last_seen_run_id，
 * 但<b>不参与 Evidence 去重</b>（去重由 {@code learning_evidence.dedupe_key} 决定）。
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

    /**
     * 跨进程串联这次执行的关联键：后端 createRun 时生成，随 {@code ChatAgentRequest}
     * 一并透传给 Python Agent 记录。
     * <p>
     * 这个项目是<b>两跳</b>（Postman → 后端 → Agent），只看 run_id 只能在各自进程里
     * 找到自己的那一半，trace_id 是唯一能把两半对上的键。
     */
    private String traceId;

    /** 失败的说明文本，不存堆栈（见 AgentStreamHandler 对 onError 的约定）。 */
    private String errorMessage;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    private LocalDateTime finishedAt;
}
