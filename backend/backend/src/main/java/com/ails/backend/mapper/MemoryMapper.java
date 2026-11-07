package com.ails.backend.mapper;

import com.ails.backend.entity.Memory;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

public interface MemoryMapper extends BaseMapper<Memory> {

    /**
     * 中文全文检索（FULLTEXT + ngram）。
     *
     * <p><b>为什么不再用 LIKE：</b>旧实现把查询切词后逐个 {@code LIKE '%term%'} 做 OR，
     * 每个 term 都是一次全表扫，且无法给出相关性；中文还要靠 Java 侧补二元组。
     * ngram 解析器在索引层完成同样的二元切分，查询只需要一条 {@code MATCH ... AGAINST}。</p>
     *
     * <p><b>语义必须与旧实现一致的部分：</b></p>
     * <ul>
     *   <li>{@code user_id} 与 {@code status = 'ACTIVE'} 始终生效；</li>
     *   <li>可见性规则：GLOBAL 对任意 spaceId 可见；SPACE 要求
     *       {@code learning_space_id = spaceId}；CONCEPT 要求 space 与 concept 同时匹配；</li>
     *   <li>{@code spaceId} 为 null 时不做可见性过滤（返回该用户全部 ACTIVE 命中）。</li>
     * </ul>
     *
     * <p><b>排序变化（有意为之）：</b>旧实现按 {@code importance, updated_at} 排序；
     * 全文检索应按相关性排序。后面两个字段只作为确定性 tie-break，避免同一分数时
     * 结果顺序不稳定。</p>
     *
     * <p><b>索引依赖：</b>必须存在 {@code FULLTEXT KEY ft_memory_content (content)
     * WITH PARSER ngram}。老库没有索引时 MySQL 会报
     * {@code Can't find FULLTEXT index matching the column list}；调用方据此降级，
     * 不要把异常吞成「没有结果」。新库由 {@code schema.sql} 建立，老库执行
     * {@code schema-migration-memory-fulltext.sql}。</p>
     *
     * <p>{@code score} 列只用于 SQL 内排序；MyBatis 会忽略这个不在实体里的列。</p>
     *
     * @param limit 结果上限。全文查询在宽泛关键词下可能命中大量行，必须有界。
     */
    @Select("""
            <script>
            SELECT m.*,
                   MATCH(m.content) AGAINST(#{query} IN NATURAL LANGUAGE MODE) AS score
            FROM memory m
            WHERE m.user_id = #{userId}
              AND m.status = 'ACTIVE'
              AND MATCH(m.content) AGAINST(#{query} IN NATURAL LANGUAGE MODE)
            <if test="spaceId != null">
              AND (
                    m.scope_type = 'GLOBAL'
                 OR (m.scope_type = 'SPACE' AND m.learning_space_id = #{spaceId})
                 <if test="conceptId != null">
                 OR (m.scope_type = 'CONCEPT' AND m.learning_space_id = #{spaceId}
                                               AND m.concept_id = #{conceptId})
                 </if>
              )
            </if>
            ORDER BY score DESC, m.importance DESC, m.updated_at DESC, m.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Memory> searchFulltext(@Param("userId") Long userId,
                               @Param("spaceId") Long spaceId,
                               @Param("conceptId") Long conceptId,
                               @Param("query") String query,
                               @Param("limit") int limit);

    /**
     * 混合检索的<b>回源复核</b>：按 id 取回 MySQL 中的权威行，并再次施加
     * {@code user_id / status = 'ACTIVE' / 可见性} 三重过滤。
     *
     * <p>这一步是整套设计的安全底线：Qdrant 里的陈旧、越权、孤儿数据都不会
     * 因为向量分高就进入结果。调用方必须丢弃本查询查不到的 id，不能只信向量库。</p>
     *
     * <p>{@code ids} 不能为空；调用方在空集合时应直接跳过查询（MySQL 的
     * {@code IN ()} 是语法错误）。</p>
     */
    @Select("""
            <script>
            SELECT m.*
            FROM memory m
            WHERE m.id IN
            <foreach collection="ids" item="id" open="(" separator="," close=")">
                #{id}
            </foreach>
              AND m.user_id = #{userId}
              AND m.status = 'ACTIVE'
            <if test="spaceId != null">
              AND (
                    m.scope_type = 'GLOBAL'
                 OR (m.scope_type = 'SPACE' AND m.learning_space_id = #{spaceId})
                 <if test="conceptId != null">
                 OR (m.scope_type = 'CONCEPT' AND m.learning_space_id = #{spaceId}
                                               AND m.concept_id = #{conceptId})
                 </if>
              )
            </if>
            </script>
            """)
    List<Memory> selectVisibleByIds(@Param("userId") Long userId,
                                    @Param("spaceId") Long spaceId,
                                    @Param("conceptId") Long conceptId,
                                    @Param("ids") List<Long> ids);

    /**
     * 空查询时的可见性列表，保持 Phase 1 之前「没输入关键词就返回 ACTIVE 列表」的语义。
     */
    @Select("""
            <script>
            SELECT m.*
            FROM memory m
            WHERE m.user_id = #{userId}
              AND m.status = 'ACTIVE'
            <if test="spaceId != null">
              AND (
                    m.scope_type = 'GLOBAL'
                 OR (m.scope_type = 'SPACE' AND m.learning_space_id = #{spaceId})
                 <if test="conceptId != null">
                 OR (m.scope_type = 'CONCEPT' AND m.learning_space_id = #{spaceId}
                                               AND m.concept_id = #{conceptId})
                 </if>
              )
            </if>
            ORDER BY m.importance DESC, m.updated_at DESC, m.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Memory> selectVisibleList(@Param("userId") Long userId,
                                   @Param("spaceId") Long spaceId,
                                   @Param("conceptId") Long conceptId,
                                   @Param("limit") int limit);
}
