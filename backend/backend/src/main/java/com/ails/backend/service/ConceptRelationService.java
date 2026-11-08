package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.concept.ConceptRelationResponse;
import com.ails.backend.dto.concept.ConceptRelationRow;
import com.ails.backend.entity.Concept;
import com.ails.backend.enums.RelationType;
import com.ails.backend.mapper.ConceptMapper;
import com.ails.backend.mapper.ConceptRelationMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.EnumSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 概念关系的<b>只读</b>查询。当前只服务于 Agent 的 Evidence 受控扩展。
 * <p>
 * 设计约束：
 * <ul>
 *   <li>没有写入入口：concept_relation 的写入路径尚未实现，本类不擅自补一套；</li>
 *   <li>作用域由“空间 + 概念”共同校验：concept_relation 没有 space_id/user_id，
 *       先要求当前用户拥有该 space，再要求被查询概念属于该 space；</li>
 *   <li>默认只开放 PREREQUISITE / RELATED；PART_OF / CONTRAST 当前不参与 Evidence 扩展；</li>
 *   <li>服务端硬上限 {@value #MAX_LIMIT} 条，调用方无法要求无限返回。</li>
 * </ul>
 *
 * <p><b>方向假设（必须与测试固定）：</b>PREREQUISITE(source, target) 表示
 * <i>source 是 target 的前置</i>。所以查询“概念 B 的前置”时，返回的是
 * target_concept_id = B 的行，其 source 以 {@code direction=INCOMING} 返回。
 * 该假设来自 schema 的 source/target 命名与现有唯一事实（仓库没有写入路径、
 * 也没有注释给出相反定义）；若未来业务确认方向相反，必须同时修改
 * {@link ConceptRelationMapper#selectRelated} 的方向映射、Agent 的筛选条件与本类测试。
 */
@Service
@RequiredArgsConstructor
public class ConceptRelationService {

    public static final int DEFAULT_LIMIT = 5;
    public static final int MAX_LIMIT = 20;

    /** 当前只允许这两类关系参与受控扩展。 */
    private static final Set<RelationType> ALLOWED_TYPES =
            EnumSet.of(RelationType.PREREQUISITE, RelationType.RELATED);
    private static final List<RelationType> DEFAULT_TYPES =
            List.of(RelationType.PREREQUISITE, RelationType.RELATED);

    private final ConceptRelationMapper relationMapper;
    private final ConceptMapper conceptMapper;
    private final LearningSpaceService spaceService;

    /**
     * 查询某概念的一跳相关概念。
     *
     * @param userId        当前登录用户
     * @param spaceId       当前学习空间；必须属于 userId
     * @param conceptId     被查询概念；必须属于 spaceId 且未软删
     * @param relationTypes 允许的类型；为空时使用 PREREQUISITE + RELATED
     * @param limit         请求条数；服务端裁剪到 [1, MAX_LIMIT]
     */
    @Transactional(readOnly = true)
    public List<ConceptRelationResponse> listRelated(
            Long userId, Long spaceId, Long conceptId,
            List<RelationType> relationTypes, Integer limit) {
        // 1) 当前用户必须拥有该空间（跨用户直接 404，不泄露空间是否存在）
        spaceService.ownedSpace(userId, spaceId);

        // 2) 被查询概念必须属于该空间且未软删（selectById 已带 @TableLogic）
        Concept concept = conceptMapper.selectById(conceptId);
        if (concept == null || !Objects.equals(concept.getLearningSpaceId(), spaceId)) {
            throw BusinessException.notFound("知识点不存在");
        }

        List<RelationType> types = normalizeTypes(relationTypes);
        int cappedLimit = normalizeLimit(limit);

        List<ConceptRelationRow> rows = relationMapper.selectRelated(
                conceptId, spaceId, types, cappedLimit);
        if (rows.isEmpty()) {
            return List.of();
        }

        // 3) 再按 relatedConceptId 回查概念：SQL 已按两端 space 过滤，这里是第二道防线，
        //    同时排除查询与回查之间被软删的概念，并拿到展示名。
        Set<Long> relatedIds = rows.stream()
                .map(ConceptRelationRow::getRelatedConceptId)
                .filter(Objects::nonNull)
                .collect(Collectors.toCollection(LinkedHashSet::new));
        if (relatedIds.isEmpty()) {
            return List.of();
        }
        Map<Long, Concept> relatedById = conceptMapper.selectList(
                        new LambdaQueryWrapper<Concept>().in(Concept::getId, relatedIds))
                .stream()
                .filter(item -> Objects.equals(item.getLearningSpaceId(), spaceId))
                .collect(Collectors.toMap(Concept::getId, Function.identity(), (a, b) -> a));

        List<ConceptRelationResponse> result = new ArrayList<>();
        for (ConceptRelationRow row : rows) {
            Concept related = relatedById.get(row.getRelatedConceptId());
            if (related == null) {
                // 跨 Space / 已软删 / 并发删除：一律不返回，不静默换成别的关系
                continue;
            }
            result.add(new ConceptRelationResponse(
                    related.getId(),
                    label(related),
                    row.getRelationType(),
                    row.getWeight(),
                    row.getDirection()));
        }
        return result;
    }

    private static List<RelationType> normalizeTypes(List<RelationType> requested) {
        if (requested == null || requested.isEmpty()) {
            return DEFAULT_TYPES;
        }
        LinkedHashSet<RelationType> result = new LinkedHashSet<>();
        for (RelationType type : requested) {
            if (type == null || !ALLOWED_TYPES.contains(type)) {
                throw new BusinessException(400, "不支持的关系类型：" + type);
            }
            result.add(type);
        }
        return result.isEmpty() ? DEFAULT_TYPES : List.copyOf(result);
    }

    private static int normalizeLimit(Integer requested) {
        if (requested == null) {
            return DEFAULT_LIMIT;
        }
        return Math.max(1, Math.min(requested, MAX_LIMIT));
    }

    private static String label(Concept concept) {
        return StringUtils.hasText(concept.getDisplayName())
                ? concept.getDisplayName()
                : concept.getCanonicalName();
    }
}
