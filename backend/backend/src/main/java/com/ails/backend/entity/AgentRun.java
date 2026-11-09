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

    /**
     * 客户端幂等键，仅 CHAT run 有值；与 {@code conversation_id} 组成唯一键。
     * <p>
     * 幂等回放时用它把「同一轮请求」的 USER 消息与 run 对上，而不是靠
     * 「对话里最新一条 CHAT run」猜测 —— 后者在并发不同 clientRequestId 时会错。
     */
    private String clientRequestId;

    /**
     * 本次 CHAT run 产出的助手消息 id，正常完成 / 截断时写入。
     * <p>
     * 消息表没有反向的 run_id，回放时需要一条明确的边来回答「这一轮的结果是哪条消息」。
     * 失败或仍在生成时为 NULL。
     */
    private Long assistantMessageId;

    private String inputSummary;

    private String outputSummary;

    /**
     * D4-3 最小上下文 Manifest 的原始 JSON（D4-4 落库）。
     * <p>
     * 只含 {@code schema_version / policy_version / intent / sources / budget}，
     * 其中每个 source 只有 {@code type / source_id / retrieval_status /
     * injection_status / reason / estimated_tokens}。刻意不含正文、完整 prompt、
     * query 或异常原文。旧数据 / 失败 run 为 {@code null}。
     * <p>
     * 用 String 承载 JSON 列，与项目其余 JSON 列（如 learning_understanding）一致；
     * 读取侧按需解析，不在实体层做自动反序列化。
     */
    private String contextManifest;

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
