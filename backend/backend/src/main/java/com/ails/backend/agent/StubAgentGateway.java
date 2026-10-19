package com.ails.backend.agent;

import com.ails.backend.dto.agent.ChatAgentRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * AgentGateway 的桩实现：把用户输入回显成一段固定的引导语，分块推出去。
 * <p>
 * 目的不是「假装有智能」，而是让整条 SSE 链路（agent_run 落库 → message.start →
 * message.delta → message 落库 → message.end）在没有 Python Agent 的情况下也能跑通、
 * 可手工验证。真正的内容分析由 {@code LearningAnalysisService} 侧的规则实现顶上。
 * <p>
 * <b>它是同步的：</b>直接在调用方线程上跑完整个循环再返回，所以
 * {@link AgentGateway} 契约里「阻塞到交互结束」「在调用方线程回调」「终局恰好一次」
 * 三条它都天然满足 —— 接真 Agent 时这三条才需要认真处理（HTTP 连接、读超时、
 * 上游 SSE 事件到 {@link AgentStreamHandler} 的翻译）。
 * <p>
 * 回显内容里<b>刻意不含</b>任何从 {@code request} 里捞出来的业务数据（不引概念、
 * 不引历史），这样「后端自己有没有算错」不会被桩回复掩盖掉：看到的每一句都是它自己写的。
 * <p>
 * <b>它与 {@link HttpPythonAgentGateway} 是二选一的。</b>两者都实现同一个接口，
 * 容器里同时存在两个候选 Bean 会启动即报 NoUniqueBeanDefinitionException，
 * 所以两边都挂 {@code @ConditionalOnProperty} 由 {@code ails.agent.gateway} 决定谁上场。
 * 桩这边带 {@code matchIfMissing = true}：配置项缺省时仍然用它，这样「刚克隆下来
 * 没装 Python 环境」也能直接启动。
 */
@Slf4j
@Component
@ConditionalOnProperty(name = "ails.agent.gateway", havingValue = "stub", matchIfMissing = true)
public class StubAgentGateway implements AgentGateway {

    /** 模拟流式输出的分块大小。取小值是为了让前端能肉眼看到逐段渲染的过程。 */
    private static final int CHUNK_SIZE = 12;

    /**
     * 把回复切块推给 handler，最后调 {@code onComplete} 收尾。
     * <p>
     * 这里的 try/catch 是整个流程唯一的异常出口：{@code handler.onDelta} /
     * {@code handler.onComplete} 抛出（例如 ChatService 那侧落库失败）都会被转成
     * {@code onError}。注意这只是「尽量把失败告知调用方」——调用方可能已经自己占掉
     * 终局名额，这时 onError 会被它丢弃，属于正常情况。
     */
    @Override
    public void streamReply(ChatAgentRequest request, AgentStreamHandler handler) {
        String reply = buildReply(request);
        try {
            // 按固定大小切片，模拟「模型逐段吐字」。最后一段可能不足 CHUNK_SIZE。
            for (int i = 0; i < reply.length(); i += CHUNK_SIZE) {
                String delta = reply.substring(i, Math.min(reply.length(), i + CHUNK_SIZE));
                handler.onDelta(delta);
            }
            handler.onComplete();
        } catch (Exception e) {
            log.error("StubAgentGateway failed, runId={}", request.runId(), e);
            handler.onError(500, "生成回复失败");
        }
    }

    /**
     * 造回复文本。只做回显，不查库、不调模型 —— 桩实现保持零依赖，
     * 这样它跑得通就说明问题不在业务逻辑而在别处。
     */
    private String buildReply(ChatAgentRequest request) {
        String userMessage = request.userMessage() == null ? "" : request.userMessage().trim();
        return "（桩回复）我收到了你说的「" + userMessage + "」。"
                + "当前后端还没有接入真正的 Agent Runtime，"
                + "这一步只用于验证对话链路与证据链路是否通畅。";
    }
}
