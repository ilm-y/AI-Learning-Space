package com.ails.backend.service;

import com.ails.backend.agent.AgentAnalysisException;
import com.ails.backend.agent.AgentAnalysisGateway;
import com.ails.backend.dto.agent.AnalyzeAgentRequest;
import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.mapper.AgentRunMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * {@link TurnAnalysisService} 的编排测试。
 * <p>
 * 这里不验证「分析出了什么证据」（那是 Agent 与写入服务的职责），只钉住本类唯一的价值：
 * <b>在 Agent 与 rules-v1 之间做选择，并且不论走哪条路都把 agent_run 收尾正确。</b>
 * 三条分支各一个用例，任何一条走错都会让 run 表出现「RUNNING 悬挂」或「重复分析」。
 */
class TurnAnalysisServiceTest {

    private AgentAnalysisGateway gateway;
    private AgentAnalysisWriteService writeService;
    private LearningAnalysisService learningAnalysisService;
    private MessageService messageService;
    private AgentRunMapper runMapper;
    private TurnAnalysisService service;

    private Conversation conversation;
    private Message userMessage;
    private LearningAnalysisService.TurnOpening opening;

    @BeforeEach
    void setUp() {
        gateway = mock(AgentAnalysisGateway.class);
        writeService = mock(AgentAnalysisWriteService.class);
        learningAnalysisService = mock(LearningAnalysisService.class);
        messageService = mock(MessageService.class);
        runMapper = mock(AgentRunMapper.class);
        // insert 之后 MyBatis-Plus 会把自增 id 写回实体；mock 里手动补一个，
        // 否则 TurnContext.agentRunId 永远是 null，断言不到真正传下去的值。
        doAnswer(invocation -> {
            invocation.getArgument(0, AgentRun.class).setId(42L);
            return 1;
        }).when(runMapper).insert(any(AgentRun.class));
        when(messageService.history(anyLong(), anyCollection(), anyInt())).thenReturn(List.of());

        service = new TurnAnalysisService(gateway, writeService, learningAnalysisService, messageService, runMapper);

        conversation = new Conversation();
        conversation.setId(100L);
        conversation.setUserId(1L);
        conversation.setLearningSpaceId(3L);
        conversation.setSessionId(20L);

        userMessage = new Message();
        userMessage.setId(1001L);
        userMessage.setContent("TLB Miss 之后呢");

        opening = new LearningAnalysisService.TurnOpening(List.of(), null);
    }

    @Test
    @DisplayName("Agent 未启用：直接走 rules-v1，不创建空的 ANALYSIS run")
    void fallsBackToRulesWhenGatewayDisabled() {
        when(gateway.enabled()).thenReturn(false);
        when(learningAnalysisService.analyzeMessage(any(), any())).thenReturn(List.of());

        List<LearningEvidence> result = service.analyzeTurn(
                conversation, userMessage, "回复", 1002L, "jwt", opening);

        assertThat(result).isEmpty();
        verify(learningAnalysisService).analyzeMessage(userMessage, opening.matched());
        verify(gateway, never()).analyze(any());
        verify(writeService, never()).apply(any(), any());
        verify(runMapper, never()).insert(any(AgentRun.class));
    }

    @Test
    @DisplayName("Agent 成功：写入结果，run 收尾 COMPLETED，且不再跑 rules-v1")
    void appliesAgentResultAndCompletesRun() {
        when(gateway.enabled()).thenReturn(true);
        AnalyzeAgentResult analysis = new AnalyzeAgentResult(
                List.of(), null, List.of(), null, "agent-v1", List.of("高速缓存"));
        when(gateway.analyze(any())).thenReturn(analysis);
        AgentAnalysisWriteService.WriteResult written =
                new AgentAnalysisWriteService.WriteResult(List.of(), 1, 2, 0, 12L);
        when(writeService.apply(any(), eq(analysis))).thenReturn(written);

        List<LearningEvidence> result = service.analyzeTurn(
                conversation, userMessage, "回复", 1002L, "jwt", opening);

        assertThat(result).isEmpty();
        ArgumentCaptor<AnalyzeAgentRequest> request = ArgumentCaptor.forClass(AnalyzeAgentRequest.class);
        verify(gateway).analyze(request.capture());
        // 分析请求必须把「本轮」与「历史」分开：用户消息与助手消息都已经在表里，
        // 历史里不能再出现它们，否则 prompt 会把同一轮对话说两遍。
        assertThat(request.getValue().messageId()).isEqualTo(1001L);
        assertThat(request.getValue().userMessage()).isEqualTo("TLB Miss 之后呢");
        assertThat(request.getValue().assistantMessage()).isEqualTo("回复");
        assertThat(request.getValue().authToken()).isEqualTo("jwt");

        ArgumentCaptor<AgentAnalysisWriteService.TurnContext> context =
                ArgumentCaptor.forClass(AgentAnalysisWriteService.TurnContext.class);
        verify(writeService).apply(context.capture(), eq(analysis));
        assertThat(context.getValue().agentRunId()).isEqualTo(42L);
        assertThat(context.getValue().learningSpaceId()).isEqualTo(3L);

        AgentRun finished = captureFinishedRun();
        assertThat(finished.getStatus()).isEqualTo(AgentRunStatus.COMPLETED);
        assertThat(finished.getOutputSummary()).contains("evidence=0").contains("test=12");
        verify(learningAnalysisService, never()).analyzeMessage(any(), any());
    }

    @Test
    @DisplayName("Agent 失败：run 收尾 FAILED，并退回 rules-v1")
    void fallsBackAndMarksRunFailedWhenAgentFails() {
        when(gateway.enabled()).thenReturn(true);
        when(gateway.analyze(any())).thenThrow(new AgentAnalysisException(503, "Agent 不可达"));
        when(learningAnalysisService.analyzeMessage(any(), any())).thenReturn(List.of());

        List<LearningEvidence> result = service.analyzeTurn(
                conversation, userMessage, "回复", 1002L, "jwt", opening);

        assertThat(result).isEmpty();
        verify(learningAnalysisService).analyzeMessage(userMessage, opening.matched());
        AgentRun finished = captureFinishedRun();
        assertThat(finished.getStatus()).isEqualTo(AgentRunStatus.FAILED);
        assertThat(finished.getErrorMessage()).contains("Agent 不可达");
        verify(writeService, never()).apply(any(), any());
    }

    /** run 的插入与收尾共用一个 mapper；收尾那次是只带 id 的 patch，按此过滤。 */
    private AgentRun captureFinishedRun() {
        ArgumentCaptor<AgentRun> captor = ArgumentCaptor.forClass(AgentRun.class);
        verify(runMapper).updateById(captor.capture());
        return captor.getValue();
    }
}
