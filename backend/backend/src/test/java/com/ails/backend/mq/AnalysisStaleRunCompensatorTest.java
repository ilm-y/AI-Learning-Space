package com.ails.backend.mq;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.mapper.AgentRunMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 兜底扫描只结束状态、不重投消息；与结果消费者的竞争由条件 UPDATE 本身裁决。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisStaleRunCompensatorTest {

    @Mock
    private AgentRunMapper agentRunMapper;

    @Test
    void disabledSweepDoesNothing() {
        AnalysisStaleRunCompensator compensator = new AnalysisStaleRunCompensator(
                agentRunMapper, new AnalysisCompensationProperties(false, 60_000L, 60_000L, 100));

        compensator.compensate();

        verify(agentRunMapper, never()).selectStaleAnalysisRuns(any(), anyInt());
    }

    @Test
    void emptySweepDoesNotUpdate() {
        AnalysisStaleRunCompensator compensator = compensator(true, 60_000L, 100);
        when(agentRunMapper.selectStaleAnalysisRuns(any(), eq(100))).thenReturn(List.of());

        compensator.compensate();

        verify(agentRunMapper, never()).markAnalysisRunStale(anyLong(), any(), any());
    }

    @Test
    void staleRunIsMarkedStaleWithCutoffFromConfiguration() {
        AnalysisStaleRunCompensator compensator = compensator(true, 3_600_000L, 100);
        AgentRun run = run(7L);
        when(agentRunMapper.selectStaleAnalysisRuns(any(), eq(100))).thenReturn(List.of(run));
        when(agentRunMapper.markAnalysisRunStale(eq(7L), any(), any())).thenReturn(1);

        compensator.compensate();

        ArgumentCaptor<LocalDateTime> cutoff = ArgumentCaptor.forClass(LocalDateTime.class);
        ArgumentCaptor<String> error = ArgumentCaptor.forClass(String.class);
        verify(agentRunMapper).markAnalysisRunStale(eq(7L), cutoff.capture(), error.capture());
        // 阈值来自配置：cutoff 应该落在「现在减 60 分钟」附近
        assertThat(cutoff.getValue()).isBefore(LocalDateTime.now().minusMinutes(59));
        assertThat(cutoff.getValue()).isAfter(LocalDateTime.now().minusMinutes(61));
        // 只是「可能遗留」的可观测标记，措辞不能让人以为任务已确认失败
        assertThat(error.getValue()).contains("STALE").contains("结果到达");
    }

    @Test
    void lostRaceWithResultConsumerIsNotAnError() {
        AnalysisStaleRunCompensator compensator = compensator(true, 60_000L, 100);
        when(agentRunMapper.selectStaleAnalysisRuns(any(), eq(100))).thenReturn(List.of(run(7L)));
        // 结果消费者先提交了终态：条件 UPDATE 命中 0 行
        when(agentRunMapper.markAnalysisRunStale(eq(7L), any(), any())).thenReturn(0);

        assertThatCode(compensator::compensate).doesNotThrowAnyException();
    }

    @Test
    void oneFailureDoesNotAbortTheRestOfTheBatch() {
        AnalysisStaleRunCompensator compensator = compensator(true, 60_000L, 100);
        when(agentRunMapper.selectStaleAnalysisRuns(any(), eq(100)))
                .thenReturn(List.of(run(7L), run(8L)));
        when(agentRunMapper.markAnalysisRunStale(eq(7L), any(), any()))
                .thenThrow(new IllegalStateException("db blip"));
        when(agentRunMapper.markAnalysisRunStale(eq(8L), any(), any())).thenReturn(1);

        compensator.compensate();

        verify(agentRunMapper).markAnalysisRunStale(eq(8L), any(), any());
    }

    private AnalysisStaleRunCompensator compensator(boolean enabled, long staleAfterMs, int batchSize) {
        return new AnalysisStaleRunCompensator(
                agentRunMapper, new AnalysisCompensationProperties(enabled, staleAfterMs, 60_000L, batchSize));
    }

    private static AgentRun run(Long id) {
        AgentRun run = new AgentRun();
        run.setId(id);
        run.setTraceId("trace-" + id);
        run.setCreatedAt(LocalDateTime.now().minusHours(2));
        return run;
    }
}
