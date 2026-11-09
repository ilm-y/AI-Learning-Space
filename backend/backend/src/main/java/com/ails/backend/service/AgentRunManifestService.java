package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.agent.AgentRunManifestPageResponse;
import com.ails.backend.dto.agent.AgentRunManifestResponse;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.ConversationMapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

/**
 * D4-4：Agent 上下文 Manifest 的只读查询。
 * <p>
 * 目标是让开发者回答「为什么 Agent 这次忘记 / 注入了某条上下文」——接口只读，
 * 写入仍由 {@link ChatService} 在 run 收尾时完成。
 * <p>
 * <b>鉴权口径：</b>
 * <ul>
 *   <li>空间必须属于当前用户（{@link LearningSpaceService#ownedSpace}）；</li>
 *   <li>run.user_id 必须等于当前用户；</li>
 *   <li>run 的 conversation 必须属于当前用户且属于路径上的 space。</li>
 * </ul>
 * 任何一项不满足都按 404 处理（与项目其余资源一致，不泄漏资源是否存在）。
 * <p>
 * <b>结构口径：</b> 响应直接暴露 D4-3 Manifest 的原始 JSON，不重命名字段、不补字段；
 * 唯一的加工是在单 run 查询时按 {@code sourceType / injectionStatus / reason}
 * 过滤 {@code sources} 数组。Manifest 本身不含正文、完整 prompt、query 或异常原文。
 */
@Service
@RequiredArgsConstructor
public class AgentRunManifestService {

    /** 分页上限：这是排查工具，不做无限量导出。 */
    private static final int MAX_PAGE_SIZE = 100;

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final AgentRunMapper agentRunMapper;
    private final ConversationMapper conversationMapper;
    private final LearningSpaceService spaceService;

    /**
     * 按 run_id 查询单次 run 的 Manifest。
     *
     * @param sourceType      过滤 source.type（Manifest 里字段名是 {@code type}）
     * @param injectionStatus 过滤 source.injection_status
     * @param reason          过滤 source.reason
     */
    @Transactional(readOnly = true)
    public AgentRunManifestResponse get(Long userId, Long spaceId, Long runId,
                                        String sourceType, String injectionStatus, String reason) {
        AgentRun run = ownedRun(userId, spaceId, runId);
        JsonNode manifest = parseManifest(run);
        JsonNode filtered = filterSources(manifest, sourceType, injectionStatus, reason);
        return response(run, spaceId, filtered);
    }

    /**
     * 按 policy_version + intent + 时间范围分页查询某空间内的 Manifest。
     * <p>
     * {@code from} 含、{@code to} 不含；两者都为 null 时不限时间。
     */
    @Transactional(readOnly = true)
    public AgentRunManifestPageResponse page(Long userId, Long spaceId,
                                             String policyVersion, String intent,
                                             LocalDateTime from, LocalDateTime to,
                                             int page, int size) {
        spaceService.ownedSpace(userId, spaceId);
        validatePage(page, size);
        if (from != null && to != null && !from.isBefore(to)) {
            throw new BusinessException(400, "from 必须早于 to");
        }

        int limit = size;
        int offset = (page - 1) * size;
        String versionFilter = StringUtils.hasText(policyVersion) ? policyVersion.trim() : null;
        String intentFilter = StringUtils.hasText(intent) ? intent.trim() : null;

        List<AgentRun> rows = agentRunMapper.selectManifestPage(
                userId, spaceId, versionFilter, intentFilter, from, to, limit, offset);
        long total = agentRunMapper.countManifestPage(
                userId, spaceId, versionFilter, intentFilter, from, to);

        List<AgentRunManifestResponse> items = new ArrayList<>(rows.size());
        for (AgentRun row : rows) {
            // 列表查询不按 source 维度过滤，直接返回完整 Manifest。
            items.add(response(row, spaceId, parseManifest(row)));
        }
        return new AgentRunManifestPageResponse(items, total, page, size);
    }

    // ---------- 归属校验 ----------

    /**
     * 取当前用户在该空间内拥有的 run。跨用户 / 跨空间 / 已软删对话一律 404。
     */
    private AgentRun ownedRun(Long userId, Long spaceId, Long runId) {
        spaceService.ownedSpace(userId, spaceId);
        AgentRun run = agentRunMapper.selectById(runId);
        if (run == null
                || !userId.equals(run.getUserId())
                || run.getConversationId() == null) {
            throw BusinessException.notFound("Agent run 不存在");
        }
        Conversation conversation = conversationMapper.selectById(run.getConversationId());
        if (conversation == null
                || !userId.equals(conversation.getUserId())
                || !spaceId.equals(conversation.getLearningSpaceId())) {
            throw BusinessException.notFound("Agent run 不存在");
        }
        return run;
    }

    // ---------- Manifest 解析 / 过滤 ----------

    private static JsonNode parseManifest(AgentRun run) {
        String json = run.getContextManifest();
        if (!StringUtils.hasText(json)) {
            throw BusinessException.notFound("该 Agent run 没有可查询的上下文 Manifest");
        }
        JsonNode manifest;
        try {
            manifest = MAPPER.readTree(json);
        } catch (Exception e) {
            // 落库时 MySQL 已校验 JSON；走到这里说明数据被外部改动或列类型不一致。
            // 不把原始内容拼进异常，避免把可能的正文片段带进日志 / 响应。
            throw new BusinessException(500, "上下文 Manifest 无法解析");
        }
        if (manifest == null || !manifest.isObject()) {
            throw new BusinessException(500, "上下文 Manifest 结构非法");
        }
        return manifest;
    }

    /**
     * 在单次 run 内过滤 sources。三个条件同时给出时取交集；空条件不参与过滤。
     * <p>
     * 只复制 Manifest 的顶层对象并替换 {@code sources}，其余字段原样保留；
     * 没有过滤条件时直接返回原对象，不做无意义的复制。
     */
    private static JsonNode filterSources(JsonNode manifest, String sourceType,
                                          String injectionStatus, String reason) {
        boolean hasFilter = StringUtils.hasText(sourceType)
                || StringUtils.hasText(injectionStatus)
                || StringUtils.hasText(reason);
        if (!hasFilter || !manifest.path("sources").isArray()) {
            return manifest;
        }

        ObjectNode copy = ((ObjectNode) manifest).deepCopy();
        ArrayNode filtered = copy.putArray("sources");
        for (JsonNode source : manifest.get("sources")) {
            if (matches(source, "type", sourceType)
                    && matches(source, "injection_status", injectionStatus)
                    && matches(source, "reason", reason)) {
                filtered.add(source);
            }
        }
        return copy;
    }

    private static boolean matches(JsonNode source, String field, String expected) {
        return !StringUtils.hasText(expected)
                || expected.trim().equals(source.path(field).asText(null));
    }

    // ---------- 响应组装 ----------

    private static AgentRunManifestResponse response(AgentRun run, Long spaceId, JsonNode manifest) {
        return new AgentRunManifestResponse(
                run.getId(),
                run.getConversationId(),
                spaceId,
                run.getStatus() == null ? null : run.getStatus().name(),
                run.getCreatedAt(),
                manifest);
    }

    private static void validatePage(int page, int size) {
        if (page < 1) {
            throw new BusinessException(400, "page 必须大于等于 1");
        }
        if (size < 1 || size > MAX_PAGE_SIZE) {
            throw new BusinessException(400, "size 必须在 1 到 " + MAX_PAGE_SIZE + " 之间");
        }
    }
}
