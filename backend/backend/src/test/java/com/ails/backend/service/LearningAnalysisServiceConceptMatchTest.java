package com.ails.backend.service;

import com.ails.backend.entity.Concept;
import com.ails.backend.entity.Conversation;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.ConversationMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * rules-v1 的 Concept 名称匹配口径。
 * <p>
 * 这里固定的是「一段文字算不算提到了某个概念」——它决定了 Evidence 会写到哪个
 * concept_id 上。与 Agent 侧 {@code concept_detector._contains} 对齐后：
 * 纯 ASCII 名称按词边界、忽略大小写匹配；中文名称仍按子串匹配。
 * <p>
 * 不连接数据库：只验证匹配结果与 Session 归属入参，写入链路仍由各自的单测覆盖。
 */
@ExtendWith(MockitoExtension.class)
class LearningAnalysisServiceConceptMatchTest {

    private static final long SPACE_ID = 7L;
    private static final long TLB_ID = 10L;
    private static final long PAGE_TABLE_ID = 11L;

    @Mock
    private EvidenceService evidenceService;
    @Mock
    private UnderstandingService understandingService;
    @Mock
    private LearningStateService learningStateService;
    @Mock
    private SessionService sessionService;
    @Mock
    private AgentRunMapper agentRunMapper;
    @Mock
    private ConceptMapper conceptMapper;
    @Mock
    private ConversationMapper conversationMapper;

    private LearningAnalysisService service;

    @BeforeEach
    void setUp() {
        service = new LearningAnalysisService(
                evidenceService,
                understandingService,
                learningStateService,
                sessionService,
                agentRunMapper,
                conceptMapper,
                conversationMapper);
    }

    @Test
    @DisplayName("小写 tlb miss 命中 TLB：不能因为大小写不同就丢掉证据")
    void lowercaseAsciiNameMatchesSameConcept() {
        when(conceptMapper.selectList(any())).thenReturn(List.of(concept(TLB_ID, "TLB", "TLB（快表）")));
        Conversation conversation = conversation();

        LearningAnalysisService.TurnOpening opening =
                service.beginTurn(conversation, "tlb miss 没命中会怎样？");

        assertThat(opening.matched()).extracting(Concept::getId).containsExactly(TLB_ID);
        // Session 归属使用的是同一个命中结果，不能在两条链路上给出不同概念。
        verify(sessionService).resolveForConversation(conversation, List.of("TLB（快表）"));
    }

    @Test
    @DisplayName("TLBSize 不命中 TLB：ASCII 名称按词边界匹配，避免把无关文本写成概念证据")
    void asciiNameRequiresWordBoundary() {
        when(conceptMapper.selectList(any())).thenReturn(List.of(concept(TLB_ID, "TLB", "TLB（快表）")));

        LearningAnalysisService.TurnOpening opening =
                service.beginTurn(conversation(), "TLBSize 一般怎么调？");

        assertThat(opening.matched()).isEmpty();
        verify(sessionService).resolveForConversation(any(), eq(List.of()));
    }

    @Test
    @DisplayName("多词 ASCII 名称仍可命中：Page Table 保持整体匹配")
    void multiWordAsciiNameStillMatches() {
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(PAGE_TABLE_ID, "Page Table", null)));

        LearningAnalysisService.TurnOpening opening =
                service.beginTurn(conversation(), "page table 和 TLB 的关系是什么？");

        assertThat(opening.matched()).extracting(Concept::getId).containsExactly(PAGE_TABLE_ID);
    }

    @Test
    @DisplayName("中文名称仍按子串匹配：页表不能因为改成词边界而漏召回")
    void chineseNameKeepsSubstringMatching() {
        when(conceptMapper.selectList(any()))
                .thenReturn(List.of(concept(PAGE_TABLE_ID, "页表", "页表")));

        LearningAnalysisService.TurnOpening opening =
                service.beginTurn(conversation(), "页表项到底存在哪里？");

        assertThat(opening.matched()).extracting(Concept::getId).containsExactly(PAGE_TABLE_ID);
    }

    private static Conversation conversation() {
        Conversation conversation = new Conversation();
        conversation.setId(1L);
        conversation.setUserId(1L);
        conversation.setLearningSpaceId(SPACE_ID);
        return conversation;
    }

    private static Concept concept(long id, String canonicalName, String displayName) {
        Concept concept = new Concept();
        concept.setId(id);
        concept.setLearningSpaceId(SPACE_ID);
        concept.setCanonicalName(canonicalName);
        concept.setDisplayName(displayName);
        return concept;
    }
}
