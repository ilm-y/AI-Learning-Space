package com.ails.backend.agent;

import com.ails.backend.dto.agent.AnalyzeAgentRequest;
import com.ails.backend.dto.agent.AnalyzeAgentResult;

/**
 * Backend 与 Agent 学习分析能力的边界。
 * <p>
 * 它与 {@link AgentGateway}（对话）<b>刻意分成两个接口</b>：
 * <ul>
 *   <li>对话是流式的、面向用户的；分析是一次性的、只写库的；</li>
 *   <li>两者的失败语义不同：对话失败要推 {@code message.error}，分析失败要静默退回规则版；</li>
 *   <li>合成一个接口会迫使每个实现都写一个「我这一侧不支持分析」的空方法。</li>
 * </ul>
 * 两个接口由同一个配置项 {@code ails.agent.gateway} 选择实现，保持「换 Agent 只改一处配置」。
 *
 * <h3>实现契约</h3>
 * <ol>
 *   <li><b>阻塞调用。</b> 方法返回时分析已经完成或失败；调用方随后会立刻写库。</li>
 *   <li><b>不写业务数据。</b> 返回的是候选集合，Evidence / Understanding / Memory / Test
 *       的落库全部由后端 {@code AgentAnalysisWriteService} 在一个事务里完成。</li>
 *   <li><b>失败抛 {@link AgentAnalysisException}，不要返回 null。</b> null 只用于
 *       {@link #enabled()} 为 false 的桩实现。</li>
 * </ol>
 */
public interface AgentAnalysisGateway {

    /**
     * 当前实现是否真的具备分析能力。
     * <p>
     * 桩实现返回 false，调用方据此直接走本地规则，而不用先发一次无意义的请求、
     * 再为它落一条空的 {@code agent_run}。
     */
    boolean enabled();

    /**
     * 分析一轮已经完成的对话。
     *
     * @return 结构化候选；即使没有学习信号也返回一个各项为空的结果，而不是 null
     * @throws AgentAnalysisException 连接失败、上游错误或响应不可解析
     */
    AnalyzeAgentResult analyze(AnalyzeAgentRequest request);
}
