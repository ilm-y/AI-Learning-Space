package com.ails.backend.mapper;

import com.ails.backend.dto.concept.ConceptRelationRow;
import com.ails.backend.entity.ConceptRelation;
import com.ails.backend.enums.RelationType;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * 概念关系。除 MyBatis-Plus 自带方法外，只有一条<b>手写只读 SQL</b>。
 * <p>
 * concept_relation 本身没有 space_id / user_id，作用域必须由两端的 concept 共同决定。
 * 因此查询时同时 join source / target 两个 concept，并强制：
 * <ul>
 *   <li>两端都属于调用方传入的 learning_space_id —— 跨 Space 的关系不会返回；</li>
 *   <li>两端都未软删 —— 已删除概念不参与扩展；</li>
 *   <li>关系类型由白名单过滤，条数由服务端上限裁剪。</li>
 * </ul>
 * 手写 SQL 不会被 {@code @TableLogic} 自动追加条件，所以这里必须显式写 deleted_at。
 */
public interface ConceptRelationMapper extends BaseMapper<ConceptRelation> {

    /**
     * 查询与 {@code conceptId} 直接相连的一跳关系。
     * <p>
     * 返回的 {@code related_concept_id} 是“另一端”，{@code direction} 表示
     * conceptId 在关系中的位置。PREREQUISITE 的语义假设见
     * {@link ConceptRelationRow}：source 是 target 的前置。
     *
     * @param conceptId     被查询概念
     * @param spaceId       当前学习空间；两端概念都必须属于它
     * @param relationTypes 已由服务层白名单过滤并裁剪过的关系类型
     * @param limit         已由服务层裁剪到 [1, MAX] 的条数
     */
    @Select("""
            <script>
            SELECT
                CASE WHEN r.source_concept_id = #{conceptId}
                     THEN r.target_concept_id ELSE r.source_concept_id END AS related_concept_id,
                r.relation_type,
                r.weight,
                CASE WHEN r.source_concept_id = #{conceptId}
                     THEN 'OUTGOING' ELSE 'INCOMING' END AS direction
            FROM concept_relation r
            JOIN concept src ON src.id = r.source_concept_id AND src.deleted_at IS NULL
            JOIN concept tgt ON tgt.id = r.target_concept_id AND tgt.deleted_at IS NULL
            WHERE (r.source_concept_id = #{conceptId} OR r.target_concept_id = #{conceptId})
              AND src.learning_space_id = #{spaceId}
              AND tgt.learning_space_id = #{spaceId}
              AND r.relation_type IN
              <foreach collection="relationTypes" item="type" open="(" separator="," close=")">
                  #{type}
              </foreach>
            ORDER BY r.weight DESC, r.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<ConceptRelationRow> selectRelated(
            @Param("conceptId") Long conceptId,
            @Param("spaceId") Long spaceId,
            @Param("relationTypes") List<RelationType> relationTypes,
            @Param("limit") int limit);
}
