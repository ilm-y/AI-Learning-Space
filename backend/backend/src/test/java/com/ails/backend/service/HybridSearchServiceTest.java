package com.ails.backend.service;

import com.ails.backend.config.MemorySearchProperties;
import com.ails.backend.dto.memory.HybridMemorySearchResponse;
import com.ails.backend.dto.memory.agent.MemoryVectorHit;
import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryHybridStatus;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.utils.Hashing;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 混合检索的降级、融合与「回源复核」单测。
 *
 * <p>执行器使用同步的 {@code Runnable::run}，让两路召回在测试里确定性地完成，
 * 但保持与生产相同的 {@link CompletableFuture} 处理路径。</p>
 */
@ExtendWith(MockitoExtension.class)
class HybridSearchServiceTest {

    private static final Long USER_ID = 1L;

    @Mock
    private MemoryMapper memoryMapper;
    @Mock
    private AgentMemoryClient agentMemoryClient;

    private HybridSearchService service;

    @BeforeEach
    void setUp() {
        // 与生产默认值一致：recallK=50、k=60、两路权重 1.0。
        MemorySearchProperties properties = new MemorySearchProperties(
                50, 10, 200, 60.0, 1.0, 1.0, 1500L, 2000L, 4, 8, 64);
        service = new HybridSearchService(memoryMapper, agentMemoryClient, properties,
                Runnable::run);
    }

    @Test
    @DisplayName("RRF 融合后回源复核：Qdrant 返回的不可见 id 被剔除")
    void recheckDropsIdsNotVisibleInMysql() {
        Memory first = memory(1L, "递归的基准条件", 0.5f);
        Memory second = memory(2L, "分治的拆分方式", 0.8f);
        when(memoryMapper.searchFulltext(eq(USER_ID), isNull(), isNull(), eq("递归"), eq(50)))
                .thenReturn(List.of(first, second));
        when(agentMemoryClient.search(any())).thenReturn(List.of(
                new MemoryVectorHit(2L, 0.90, Hashing.sha256Hex(second.getContent())),
                new MemoryVectorHit(99L, 0.95, "foreign-user-point")));
        // 模拟 SQL 复核：id=99 属于别的用户/已归档，SQL 不会返回。
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenReturn(List.of(first, second));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.OK);
        assertThat(response.items()).extracting(hit -> hit.memory().id())
                .containsExactly(2L, 1L);
        assertThat(response.items()).noneMatch(hit -> hit.memory().id() == 99L);

        // 证明 99 确实进入了回源候选，只是在 SQL 复核阶段被丢弃，而不是没被融合。
        @SuppressWarnings("unchecked")
        ArgumentCaptor<List<Long>> idsCaptor = ArgumentCaptor.forClass(List.class);
        verify(memoryMapper).selectVisibleByIds(eq(USER_ID), isNull(), isNull(),
                idsCaptor.capture());
        assertThat(idsCaptor.getValue()).contains(99L, 1L, 2L);
    }

    @Test
    @DisplayName("Qdrant 只返回非法 user_id 的数据：复核后结果为空，但不报错")
    void illegalCrossUserVectorHitAloneYieldsEmptyResult() {
        when(memoryMapper.searchFulltext(any(), any(), any(), anyString(), eq(50)))
                .thenReturn(List.of());
        when(agentMemoryClient.search(any())).thenReturn(List.of(
                new MemoryVectorHit(99L, 0.99, "someone-else")));
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenReturn(List.of());

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.OK);
        assertThat(response.items()).isEmpty();
        verify(memoryMapper).selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList());
    }

    @Test
    @DisplayName("Agent 不可用：降级为纯 MySQL 词法检索，不抛异常")
    void agentUnavailableDegradesToLexical() {
        Memory hit = memory(1L, "递归的基准条件", 0.5f);
        when(memoryMapper.searchFulltext(eq(USER_ID), isNull(), isNull(), eq("递归"), eq(50)))
                .thenReturn(List.of(hit));
        when(agentMemoryClient.search(any()))
                .thenThrow(new AgentMemoryException(503, "Agent 服务未启动或无法连接"));
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenReturn(List.of(hit));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.DEGRADED_VECTOR);
        assertThat(response.items()).extracting(item -> item.memory().id())
                .containsExactly(1L);
        assertThat(response.warnings()).singleElement()
                .asString().contains("向量检索失败");
    }

    @Test
    @DisplayName("MySQL 词法失败但向量可用：降级为纯向量检索")
    void lexicalFailureDegradesToVector() {
        Memory hit = memory(1L, "递归的基准条件", 0.5f);
        when(memoryMapper.searchFulltext(any(), any(), any(), anyString(), eq(50)))
                .thenThrow(new IllegalStateException("FULLTEXT 不可用"));
        when(agentMemoryClient.search(any())).thenReturn(List.of(
                new MemoryVectorHit(1L, 0.88, Hashing.sha256Hex(hit.getContent()))));
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenReturn(List.of(hit));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.DEGRADED_KEYWORD);
        assertThat(response.items()).extracting(item -> item.memory().id())
                .containsExactly(1L);
        assertThat(response.warnings()).singleElement()
                .asString().contains("词法检索失败");
    }

    @Test
    @DisplayName("两路都失败：返回 UNAVAILABLE，不把不可用伪装成空结果")
    void bothChannelsFailedReturnsUnavailable() {
        when(memoryMapper.searchFulltext(any(), any(), any(), anyString(), eq(50)))
                .thenThrow(new IllegalStateException("DB down"));
        when(agentMemoryClient.search(any()))
                .thenThrow(new AgentMemoryException(503, "Agent down"));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.UNAVAILABLE);
        assertThat(response.items()).isEmpty();
        assertThat(response.warnings()).hasSize(2);
        verify(memoryMapper, never()).selectVisibleByIds(any(), any(), any(), anyList());
    }

    @Test
    @DisplayName("空关键词：沿用旧语义返回 ACTIVE 列表，不调用 Agent")
    void blankKeywordReturnsVisibleList() {
        Memory first = memory(1L, "a", 0.5f);
        Memory second = memory(2L, "b", 0.7f);
        when(memoryMapper.selectVisibleList(eq(USER_ID), isNull(), isNull(), eq(10)))
                .thenReturn(List.of(second, first));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "   ", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.BLANK_QUERY);
        assertThat(response.items()).extracting(item -> item.memory().id())
                .containsExactly(2L, 1L);
        verifyNoInteractions(agentMemoryClient);
    }

    @Test
    @DisplayName("回源复核失败：放弃本次结果并返回 UNAVAILABLE，绝不返回未复核数据")
    void recheckFailureReturnsUnavailable() {
        Memory hit = memory(1L, "递归的基准条件", 0.5f);
        when(memoryMapper.searchFulltext(any(), any(), any(), anyString(), eq(50)))
                .thenReturn(List.of(hit));
        when(agentMemoryClient.search(any())).thenReturn(List.of());
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenThrow(new IllegalStateException("replica unavailable"));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.UNAVAILABLE);
        assertThat(response.items()).isEmpty();
        assertThat(response.warnings()).anySatisfy(warning ->
                assertThat(warning).contains("回源复核失败"));
    }

    @Test
    @DisplayName("向量 contentHash 过期：结果仍使用 MySQL 正文，不泄漏陈旧内容")
    void staleVectorHashNeverLeaksOldContent() {
        Memory current = memory(1L, "MySQL 中的最新内容", 0.5f);
        when(memoryMapper.searchFulltext(any(), any(), any(), anyString(), eq(50)))
                .thenReturn(List.of(current));
        when(agentMemoryClient.search(any())).thenReturn(List.of(
                new MemoryVectorHit(1L, 0.9, "hash-of-an-old-revision")));
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenReturn(List.of(current));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", null);

        assertThat(response.status()).isEqualTo(MemoryHybridStatus.OK);
        assertThat(response.items()).singleElement()
                .satisfies(item -> {
                    assertThat(item.memory().content()).isEqualTo("MySQL 中的最新内容");
                    assertThat(item.vectorScore()).isEqualTo(0.9);
                });
    }

    @Test
    @DisplayName("limit：请求条数在 RRF 排序之后截断")
    void requestedLimitTruncatesAfterFusion() {
        Memory first = memory(1L, "a", 0.5f);
        Memory second = memory(2L, "b", 0.5f);
        when(memoryMapper.searchFulltext(any(), any(), any(), anyString(), eq(50)))
                .thenReturn(List.of(first, second));
        when(agentMemoryClient.search(any())).thenReturn(List.of());
        when(memoryMapper.selectVisibleByIds(eq(USER_ID), isNull(), isNull(), anyList()))
                .thenReturn(List.of(first, second));

        HybridMemorySearchResponse response =
                service.search(USER_ID, null, null, "递归", 1);

        assertThat(response.items()).hasSize(1);
        assertThat(response.items().get(0).memory().id()).isEqualTo(1L);
    }

    private static Memory memory(long id, String content, float importance) {
        Memory memory = new Memory();
        memory.setId(id);
        memory.setUserId(USER_ID);
        memory.setScopeType(MemoryScopeType.GLOBAL);
        memory.setScopeKey("GLOBAL");
        memory.setMemoryKey("k-" + id);
        memory.setMemoryType(MemoryType.LEARNING);
        memory.setContent(content);
        memory.setImportance(importance);
        memory.setStatus(MemoryStatus.ACTIVE);
        memory.setVersion(0);
        return memory;
    }
}
