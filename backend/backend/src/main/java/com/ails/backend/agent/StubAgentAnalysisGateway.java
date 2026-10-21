package com.ails.backend.agent;

import com.ails.backend.dto.agent.AnalyzeAgentRequest;
import com.ails.backend.dto.agent.AnalyzeAgentResult;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * 分析网关的桩实现：不调用任何外部进程。
 * <p>
 * {@link #enabled()} 返回 false，调用方会直接走 {@code LearningAnalysisService} 的
 * {@code rules-v1}。这样在默认 {@code gateway=stub} 配置下：
 * <ul>
 *   <li>对话仍然能产生 Evidence（规则版按概念名匹配）；</li>
 *   <li>不会为每条消息创建一条空的 ANALYSIS run；</li>
 *   <li>不需要 Python 环境也能启动与跑测试。</li>
 * </ul>
 * 与 {@link StubAgentGateway} 一样带 {@code matchIfMissing = true}，保证新克隆的仓库开箱即用。
 */
@Component
@ConditionalOnProperty(name = "ails.agent.gateway", havingValue = "stub", matchIfMissing = true)
public class StubAgentAnalysisGateway implements AgentAnalysisGateway {

    @Override
    public boolean enabled() {
        return false;
    }

    @Override
    public AnalyzeAgentResult analyze(AnalyzeAgentRequest request) {
        // enabled() 已经明确为 false，调用方不应走到这里；真走到了也不假装成功。
        throw new AgentAnalysisException(501, "Agent 分析网关未启用");
    }
}
