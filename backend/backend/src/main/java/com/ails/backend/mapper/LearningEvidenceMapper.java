package com.ails.backend.mapper;

import com.ails.backend.entity.LearningEvidence;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

public interface LearningEvidenceMapper extends BaseMapper<LearningEvidence> {

    /**
     * 跨概念的有界证据查询：SQL 内完成过滤、倒序与 LIMIT。
     * <p>
     * 不允许「先全量拉取再在应用层切片」：证据是 append-only 且会持续增长，
     * 全量读取会随用户历史线性变慢。{@code concept_id IN (...)} 与
     * {@code ORDER BY created_at DESC, id DESC} 一起构成「最近 N 条」的唯一定义。
     * <p>
     * {@code userId} 与 {@code spaceId} 都是必填作用域。Space 条件通过
     * {@code JOIN concept} 下沉到 SQL：{@code learning_evidence} 本身没有
     * {@code space_id} 列，证据的空间归属由它挂载的 concept 决定。Service 层的
     * {@code ownedSpace} 与 concept 归属预校验仍然保留，这里是纵深防御，保证
     * 即使未来新增调用方绕过预校验，SQL 也不会跨 Space 返回证据。
     */
    @Select("""
            <script>
            SELECT e.*
            FROM learning_evidence e
            JOIN concept c ON c.id = e.concept_id
            WHERE e.user_id = #{userId}
              AND c.learning_space_id = #{spaceId}
              AND e.concept_id IN
              <foreach collection="conceptIds" item="conceptId" open="(" separator="," close=")">
                  #{conceptId}
              </foreach>
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT #{limit}
            </script>
            """)
    List<LearningEvidence> selectRecentByConcepts(
            @Param("userId") Long userId,
            @Param("spaceId") Long spaceId,
            @Param("conceptIds") List<Long> conceptIds,
            @Param("limit") int limit);
}
