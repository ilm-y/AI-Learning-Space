package com.ails.backend.mq;

import com.ails.backend.dto.agent.AnalyzeAgentResult;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.MessageMapper;
import com.ails.backend.service.AgentAnalysisWriteService;
import com.ails.backend.service.LearningAnalysisService;
import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 结果消费者的业务规则：不启动 Spring / RabbitMQ，只验证幂等与失败分支。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisResultProcessorTest {

    @Mock
    private AgentRunMapper agentRunMapper;
    @Mock
    private ConversationMapper conversationMapper;
    @Mock
    private MessageMapper messageMapper;
    @Mock
    private AgentAnalysisWriteService writeService;
    @Mock
    private LearningAnalysisService learningAnalysisService;

    @InjectMocks
    private AnalysisResultProcessor processor;

    /**
     * LambdaUpdateWrapper 在拼 SQL 时需要 MyBatis-Plus 的 TableInfo 缓存；
     * 纯 Mockito 测试不会启动 MyBatis，所以手动初始化一次实体元数据。
     */
    @BeforeAll
    static void initTableInfo() {
        MapperBuilderAssistant assistant =
                new MapperBuilderAssistant(new MybatisConfiguration(), "");
        TableInfoHelper.initTableInfo(assistant, AgentRun.class);
    }

    @Test
    void appliesResultAndMarksSucceeded() {
        AgentRun run = analysisRun(AgentRunStatus.PENDING);
        Conversation conversation = conversation(run);
        Message userMessage = userMessage(conversation);
        when(agentRunMapper.update(any(), any())).thenReturn(1);
        when(agentRunMapper.selectOne(any())).thenReturn(run);
        when(conversationMapper.selectById(conversation.getId())).thenReturn(conversation);
        when(messageMapper.selectById(userMessage.getId())).thenReturn(userMessage);
        when(writeService.apply(any(), any())).thenReturn(
                new AgentAnalysisWriteService.WriteResult(List.of(), 1, 0, 0, null));

        AnalysisResultProcessor.Outcome outcome = processor.process(successMessage(run, userMessage));

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.APPLIED);
        verify(writeService).apply(any(), any());
        // 一次抢占 + 一次收尾
        verify(agentRunMapper, org.mockito.Mockito.times(2)).update(any(), any());
    }

    @Test
    void staleRunStillAcceptsALateResult() {
        // 补偿扫描只标记「可能遗留」，不能把仍会到达的合法结果挡在门外。
        AgentRun run = analysisRun(AgentRunStatus.STALE);
        Conversation conversation = conversation(run);
        Message userMessage = userMessage(conversation);
        when(agentRunMapper.update(any(), any())).thenReturn(1);
        when(agentRunMapper.selectOne(any())).thenReturn(run);
        when(conversationMapper.selectById(conversation.getId())).thenReturn(conversation);
        when(messageMapper.selectById(userMessage.getId())).thenReturn(userMessage);
        when(writeService.apply(any(), any())).thenReturn(
                new AgentAnalysisWriteService.WriteResult(List.of(), 1, 0, 0, null));

        AnalysisResultProcessor.Outcome outcome = processor.process(successMessage(run, userMessage));

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.APPLIED);
        verify(writeService).apply(any(), any());
    }

    @Test
    @SuppressWarnings({"unchecked", "rawtypes"})
    void claimAcceptsPendingAndStaleButNotTerminal() {
        AgentRun run = analysisRun(AgentRunStatus.PENDING);
        when(agentRunMapper.update(any(), any())).thenReturn(1);
        when(agentRunMapper.selectOne(any())).thenReturn(run);
        when(conversationMapper.selectById(any())).thenReturn(null);

        processor.process(successMessage(run, null));

        ArgumentCaptor<com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper<AgentRun>>
                captor = ArgumentCaptor.forClass(
                        (Class) com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper.class);
        verify(agentRunMapper, org.mockito.Mockito.times(2)).update(any(), captor.capture());
        // 第一条是抢占：PENDING / STALE 都可以变成 RUNNING，终态不在条件里
        var claim = captor.getAllValues().get(0);
        assertThat(claim.getSqlSet()).contains("status");
        // 先取 SQL 片段：MyBatis-Plus 的 IN 值是在渲染时才写进参数表的
        assertThat(claim.getSqlSegment()).contains("status IN");
        assertThat(claim.getParamNameValuePairs().values())
                .contains(AgentRunStatus.PENDING, AgentRunStatus.STALE);
    }

    @Test
    void duplicateResultIsAckedWithoutWritingAgain() {
        AgentRun run = analysisRun(AgentRunStatus.SUCCEEDED);
        when(agentRunMapper.update(any(), any())).thenReturn(0);
        when(agentRunMapper.selectOne(any())).thenReturn(run);

        AnalysisResultProcessor.Outcome outcome = processor.process(successMessage(run, null));

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.DUPLICATE);
        verify(writeService, never()).apply(any(), any());
    }

    @Test
    void failedResultMarksRunFailedWithoutWritingEvidence() {
        AgentRun run = analysisRun(AgentRunStatus.PENDING);
        when(agentRunMapper.update(any(), any())).thenReturn(1);
        when(agentRunMapper.selectOne(any())).thenReturn(run);

        AnalysisResultMessage message = new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION,
                run.getId(),
                run.getTraceId(),
                100L,
                null,
                AnalysisResultMessage.Status.FAILED,
                null,
                "RETRIEVAL_UNAVAILABLE",
                "backend down",
                true);

        AnalysisResultProcessor.Outcome outcome = processor.process(message);

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.FAILED);
        verify(writeService, never()).apply(any(), any());
    }

    @Test
    void traceIdMismatchIsRejected() {
        AgentRun run = analysisRun(AgentRunStatus.PENDING);
        when(agentRunMapper.update(any(), any())).thenReturn(1);
        when(agentRunMapper.selectOne(any())).thenReturn(run);

        AnalysisResultMessage message = new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION,
                run.getId(),
                "another-trace",
                100L,
                null,
                AnalysisResultMessage.Status.SUCCEEDED,
                emptyResult(),
                null, null, null);

        AnalysisResultProcessor.Outcome outcome = processor.process(message);

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.FAILED);
        verify(writeService, never()).apply(any(), any());
    }

    @Test
    void messageFromAnotherConversationIsRejected() {
        AgentRun run = analysisRun(AgentRunStatus.PENDING);
        Conversation conversation = conversation(run);
        Message foreign = userMessage(conversation);
        foreign.setConversationId(999L);
        when(agentRunMapper.update(any(), any())).thenReturn(1);
        when(agentRunMapper.selectOne(any())).thenReturn(run);
        when(conversationMapper.selectById(conversation.getId())).thenReturn(conversation);
        when(messageMapper.selectById(foreign.getId())).thenReturn(foreign);

        AnalysisResultProcessor.Outcome outcome = processor.process(successMessage(run, foreign));

        assertThat(outcome).isEqualTo(AnalysisResultProcessor.Outcome.FAILED);
        verify(writeService, never()).apply(any(), any());
    }

    // ================= fixtures =================

    private static AgentRun analysisRun(AgentRunStatus status) {
        AgentRun run = new AgentRun();
        run.setId(42L);
        run.setUserId(7L);
        run.setConversationId(9L);
        run.setRunType(AgentRunType.ANALYSIS);
        run.setStatus(status);
        run.setTraceId("trace-1");
        run.setCreatedAt(LocalDateTime.now().minusSeconds(2));
        return run;
    }

    private static Conversation conversation(AgentRun run) {
        Conversation conversation = new Conversation();
        conversation.setId(run.getConversationId());
        conversation.setUserId(run.getUserId());
        conversation.setLearningSpaceId(3L);
        return conversation;
    }

    private static Message userMessage(Conversation conversation) {
        Message message = new Message();
        message.setId(100L);
        message.setConversationId(conversation.getId());
        message.setContent("TLB 是什么？");
        return message;
    }

    private static AnalysisResultMessage successMessage(AgentRun run, Message userMessage) {
        return new AnalysisResultMessage(
                AnalysisResultMessage.SCHEMA_VERSION,
                run.getId(),
                run.getTraceId(),
                userMessage == null ? 100L : userMessage.getId(),
                null,
                AnalysisResultMessage.Status.SUCCEEDED,
                emptyResult(),
                null, null, null);
    }

    private static AnalyzeAgentResult emptyResult() {
        return new AnalyzeAgentResult(List.of(), null, List.of(), null, "agent-v1", List.of());
    }
}
