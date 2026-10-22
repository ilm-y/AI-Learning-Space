package com.ails.backend.mq;

import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * Python Agent Worker → RabbitMQ → Spring Boot 的分析结果消息。
 * <p>
 * <b>这只是候选结果，不是已经完成的写入。</b> Spring Boot 收到后仍会：
 * <ol>
 *   <li>校验 taskId / traceId / run 类型与状态；</li>
 *   <li>从数据库重新加载 conversation 与 message，重建
 *       {@link com.ails.backend.service.AgentAnalysisWriteService.TurnContext}；</li>
 *   <li>再执行一遍 conceptId 空间归属校验、Evidence 去重、Understanding 生命周期。</li>
 * </ol>
 * 不能因为 Python 返回了合法 JSON 就相信其中每一个概念判断。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AnalysisResultMessage(
        int schemaVersion,

        /** 关联的 {@code agent_run.id}。 */
        Long taskId,

        /** 必须与 run.traceId 一致；不一致说明消息串了。 */
        String traceId,

        /** Worker 从请求消息里原样带回，供重新加载来源消息。 */
        Long messageId,
        Long assistantMessageId,

        /** SUCCEEDED / FAILED。 */
        Status status,

        /** status = SUCCEEDED 时必填。 */
        AnalyzeAgentResult result,

        /** 失败分类，便于排查与决定是否需要人工补偿。 */
        String errorCode,
        String errorMessage,

        /** Worker 判断这次失败是否值得重试；仅用于日志与运维口径。 */
        Boolean retryable) {

    public static final int SCHEMA_VERSION = 1;

    public enum Status {
        SUCCEEDED,
        FAILED
    }
}
