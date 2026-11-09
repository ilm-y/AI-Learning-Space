package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.agent.AgentRunManifestPageResponse;
import com.ails.backend.dto.agent.AgentRunManifestResponse;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.utils.JsonUtil;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.LocalDateTime;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * D4-4：Manifest 只读查询的归属校验、过滤与返回结构。
 * <p>
 * 不连数据库：Mapper 用 Mockito 替身。真正执行 SQL 的跨用户 / 跨空间过滤效果
 * 由 {@code AgentRunMapperManifestSqlTest} 固定 SQL 形状；这里固定 Service 层的
 * 第二道校验与响应形状。
 */
class AgentRunManifestServiceTest {

    private static final long USER_ID = 1L;
    private static final long OTHER_USER_ID = 2L;
    private static final long SPACE_ID = 3L;
    private static final long OTHER_SPACE_ID = 4L;
    private static final long CONVERSATION_ID = 10L;
    private static final long RUN_ID = 20L;

    private static final String MANIFEST = """
            {
              "schema_version": 1,
              "policy_version": "d4-2-v1",
              "intent": "LEARNING_EXPLANATION",
              "sources": [
                {"type":"knowledge","source_id":"chunk:1","retrieval_status":"SUCCESS",
                 "injection_status":"SELECTED","reason":"within_budget","estimated_tokens":12},
                {"type":"knowledge","source_id":"chunk:2","retrieval_status":"SUCCESS",
                 "injection_status":"OMITTED","reason":"type_budget","estimated_tokens":30},
                {"type":"memory","source_id":"memory:7","retrieval_status":"SUCCESS",
                 "injection_status":"EXCLUDED","reason":"policy_skip","estimated_tokens":8}
              ],
              "budget": {"total":3000,"effective":2700,"used":412,"remaining":2588,
                         "output_reserve":300,"per_type":{"knowledge":{"cap":1080,"used":12,
                         "required":false,"injection_status":"PARTIAL"}}}
            }
            """;

    private AgentRunMapper agentRunMapper;
    private ConversationMapper conversationMapper;
    private LearningSpaceService spaceService;
    private AgentRunManifestService service;

    @BeforeEach
    void setUp() {
        agentRunMapper = mock(AgentRunMapper.class);
        conversationMapper = mock(ConversationMapper.class);
        spaceService = mock(LearningSpaceService.class);
        service = new AgentRunManifestService(agentRunMapper, conversationMapper, spaceService);
    }

    @Test
    @DisplayName("按 run_id 查询：直接返回 D4-3 Manifest，不补字段、不带正文")
    void returnsManifestWithoutExtraFields() throws Exception {
        givenOwnedRun(MANIFEST);

        AgentRunManifestResponse response =
                service.get(USER_ID, SPACE_ID, RUN_ID, null, null, null);

        assertThat(response.runId()).isEqualTo(RUN_ID);
        assertThat(response.conversationId()).isEqualTo(CONVERSATION_ID);
        assertThat(response.spaceId()).isEqualTo(SPACE_ID);
        assertThat(response.status()).isEqualTo("COMPLETED");

        JsonNode manifest = response.manifest();
        assertThat(fieldNames(manifest))
                .containsExactlyInAnyOrder("schema_version", "policy_version", "intent", "sources", "budget");
        assertThat(manifest.get("sources")).hasSize(3);

        // 隐私边界：响应里没有 run 摘要 / 错误原文 / 正文 / prompt / query。
        String json = JsonUtil.toJson(response);
        assertThat(json)
                .doesNotContain("inputSummary", "outputSummary", "errorMessage",
                        "systemInstruction", "messages", "content", "query", "preview");
        assertThat(fieldNames(manifest.get("sources").get(0)))
                .containsExactlyInAnyOrder("type", "source_id", "retrieval_status",
                        "injection_status", "reason", "estimated_tokens");
    }

    @Test
    @DisplayName("单次 run 内按 sourceType / injectionStatus / reason 过滤 sources")
    void filtersSourcesWithinSingleRun() {
        givenOwnedRun(MANIFEST);

        JsonNode manifest = service.get(USER_ID, SPACE_ID, RUN_ID,
                "knowledge", "SELECTED", "within_budget").manifest();

        assertThat(manifest.get("sources")).hasSize(1);
        assertThat(manifest.get("sources").get(0).get("source_id").asText()).isEqualTo("chunk:1");
        // budget 等其余字段原样保留。
        assertThat(manifest.get("budget").get("used").asInt()).isEqualTo(412);
    }

    @Test
    @DisplayName("过滤条件为空时不做过滤；只给一个条件时只按它过滤")
    void partialFiltersAreAppliedIndependently() {
        givenOwnedRun(MANIFEST);

        JsonNode byType = service.get(USER_ID, SPACE_ID, RUN_ID,
                "knowledge", null, null).manifest();
        assertThat(byType.get("sources")).hasSize(2);

        JsonNode byInjection = service.get(USER_ID, SPACE_ID, RUN_ID,
                null, "EXCLUDED", null).manifest();
        assertThat(byInjection.get("sources")).hasSize(1);
        assertThat(byInjection.get("sources").get(0).get("source_id").asText()).isEqualTo("memory:7");
    }

    @Test
    @DisplayName("跨用户查询被拒绝（404）")
    void rejectsCrossUserRun() {
        AgentRun run = run(MANIFEST);
        run.setUserId(OTHER_USER_ID);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(run);

        Throwable thrown = catchThrowable(() ->
                service.get(USER_ID, SPACE_ID, RUN_ID, null, null, null));

        assertThat(thrown).isInstanceOf(BusinessException.class);
        assertThat(((BusinessException) thrown).getCode()).isEqualTo(404);
        verify(conversationMapper, never()).selectById(CONVERSATION_ID);
    }

    @Test
    @DisplayName("跨空间查询被拒绝：run 的对话不属于路径上的空间（404）")
    void rejectsCrossSpaceConversation() {
        AgentRun run = run(MANIFEST);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(run);
        Conversation conversation = conversation(USER_ID, OTHER_SPACE_ID);
        when(conversationMapper.selectById(CONVERSATION_ID)).thenReturn(conversation);

        Throwable thrown = catchThrowable(() ->
                service.get(USER_ID, SPACE_ID, RUN_ID, null, null, null));

        assertThat(thrown).isInstanceOf(BusinessException.class);
        assertThat(((BusinessException) thrown).getCode()).isEqualTo(404);
    }

    @Test
    @DisplayName("空间不属于当前用户时被拒绝（404）")
    void rejectsSpaceNotOwned() {
        when(spaceService.ownedSpace(USER_ID, SPACE_ID))
                .thenThrow(BusinessException.notFound("学习空间不存在"));

        Throwable thrown = catchThrowable(() ->
                service.page(USER_ID, SPACE_ID, null, null, null, null, 1, 20));

        assertThat(thrown).isInstanceOf(BusinessException.class);
        assertThat(((BusinessException) thrown).getCode()).isEqualTo(404);
        verifyNoInteractions(agentRunMapper);
    }

    @Test
    @DisplayName("对话已软删（selectById 返回 null）时视为不存在（404）")
    void rejectsSoftDeletedConversation() {
        AgentRun run = run(MANIFEST);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(run);
        when(conversationMapper.selectById(CONVERSATION_ID)).thenReturn(null);

        Throwable thrown = catchThrowable(() ->
                service.get(USER_ID, SPACE_ID, RUN_ID, null, null, null));

        assertThat(thrown).isInstanceOf(BusinessException.class);
        assertThat(((BusinessException) thrown).getCode()).isEqualTo(404);
    }

    @Test
    @DisplayName("run 没有 Manifest 时返回 404，不返回空对象")
    void rejectsRunWithoutManifest() {
        AgentRun run = run(null);
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(run);
        when(conversationMapper.selectById(CONVERSATION_ID))
                .thenReturn(conversation(USER_ID, SPACE_ID));

        Throwable thrown = catchThrowable(() ->
                service.get(USER_ID, SPACE_ID, RUN_ID, null, null, null));

        assertThat(thrown).isInstanceOf(BusinessException.class);
        assertThat(((BusinessException) thrown).getCode()).isEqualTo(404);
    }

    @Test
    @DisplayName("按 policy_version + intent + 时间范围分页：参数下推并返回 total")
    void pagesWithFilters() {
        LocalDateTime from = LocalDateTime.of(2026, 11, 1, 0, 0);
        LocalDateTime to = LocalDateTime.of(2026, 11, 9, 0, 0);
        AgentRun first = run(MANIFEST);
        first.setId(21L);
        AgentRun second = run(MANIFEST);
        second.setId(22L);
        when(agentRunMapper.selectManifestPage(
                USER_ID, SPACE_ID, "d4-2-v1", "LEARNING_EXPLANATION", from, to, 20, 20))
                .thenReturn(List.of(first, second));
        when(agentRunMapper.countManifestPage(
                USER_ID, SPACE_ID, "d4-2-v1", "LEARNING_EXPLANATION", from, to))
                .thenReturn(2L);

        AgentRunManifestPageResponse response = service.page(
                USER_ID, SPACE_ID, " d4-2-v1 ", "LEARNING_EXPLANATION", from, to, 2, 20);

        assertThat(response.total()).isEqualTo(2);
        assertThat(response.page()).isEqualTo(2);
        assertThat(response.size()).isEqualTo(20);
        assertThat(response.items()).hasSize(2);
        assertThat(response.items().get(0).manifest().get("policy_version").asText())
                .isEqualTo("d4-2-v1");

        ArgumentCaptor<String> version = ArgumentCaptor.forClass(String.class);
        verify(agentRunMapper).selectManifestPage(
                eq(USER_ID), eq(SPACE_ID), version.capture(), eq("LEARNING_EXPLANATION"),
                eq(from), eq(to), eq(20), eq(20));
        assertThat(version.getValue()).isEqualTo("d4-2-v1");
    }

    @Test
    @DisplayName("非法分页参数 / 时间范围被拒绝（400）")
    void rejectsInvalidPaging() {
        when(spaceService.ownedSpace(USER_ID, SPACE_ID)).thenReturn(null);

        assertThat(((BusinessException) catchThrowable(() ->
                service.page(USER_ID, SPACE_ID, null, null, null, null, 0, 20))).getCode())
                .isEqualTo(400);
        assertThat(((BusinessException) catchThrowable(() ->
                service.page(USER_ID, SPACE_ID, null, null, null, null, 1, 101))).getCode())
                .isEqualTo(400);

        LocalDateTime from = LocalDateTime.of(2026, 11, 9, 0, 0);
        LocalDateTime to = LocalDateTime.of(2026, 11, 1, 0, 0);
        assertThat(((BusinessException) catchThrowable(() ->
                service.page(USER_ID, SPACE_ID, null, null, from, to, 1, 20))).getCode())
                .isEqualTo(400);
    }

    // ---------- 脚手架 ----------

    private void givenOwnedRun(String manifest) {
        when(agentRunMapper.selectById(RUN_ID)).thenReturn(run(manifest));
        when(conversationMapper.selectById(CONVERSATION_ID))
                .thenReturn(conversation(USER_ID, SPACE_ID));
    }

    private static AgentRun run(String manifest) {
        AgentRun run = new AgentRun();
        run.setId(RUN_ID);
        run.setUserId(USER_ID);
        run.setConversationId(CONVERSATION_ID);
        run.setStatus(AgentRunStatus.COMPLETED);
        run.setCreatedAt(LocalDateTime.of(2026, 11, 8, 12, 0));
        run.setContextManifest(manifest);
        return run;
    }

    private static Conversation conversation(long userId, long spaceId) {
        Conversation conversation = new Conversation();
        conversation.setId(CONVERSATION_ID);
        conversation.setUserId(userId);
        conversation.setLearningSpaceId(spaceId);
        return conversation;
    }

    private static Set<String> fieldNames(JsonNode node) {
        Set<String> names = new HashSet<>();
        node.fieldNames().forEachRemaining(names::add);
        return names;
    }
}
