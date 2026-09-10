package com.ails.backend.agent;

import com.ails.backend.dto.agent.ChatAgentRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

/**
 * AgentGateway 的桩实现：把用户输入回显成一段固定的引导语，分块推出去。
 * <p>
 * 目的不是「假装有智能」，而是让整条 SSE 链路（agent_run 落库 → message.start →
 * message.delta → message 落库 → message.end）在没有 Python Agent 的情况下也能跑通、
 * 可手工验证。真正的内容分析由 LearningAnalysisService 侧的规则实现顶上。
 */
@Slf4j
@Component
public class StubAgentGateway implements AgentGateway {

    /** 模拟流式输出的分块大小 */
    private static final int CHUNK_SIZE = 12;

    @Override
    public void streamReply(ChatAgentRequest request, AgentStreamHandler handler) {
        String reply = buildReply(request);
        try {
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

    private String buildReply(ChatAgentRequest request) {
        String userMessage = request.userMessage() == null ? "" : request.userMessage().trim();
        return "（桩回复）我收到了你说的「" + userMessage + "」。"
                + "当前后端还没有接入真正的 Agent Runtime，"
                + "这一步只用于验证对话链路与证据链路是否通畅。";
    }
}
