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
     * {@code userId} 是必填作用域；{@code conceptIds} 必须已由 Service 校验
     * 属于同一 Space，SQL 不负责推断权限。
     */
    @Select("""
            <script>
            SELECT *
            FROM learning_evidence
            WHERE user_id = #{userId}
              AND concept_id IN
              <foreach collection="conceptIds" item="conceptId" open="(" separator="," close=")">
                  #{conceptId}
              </foreach>
            ORDER BY created_at DESC, id DESC
            LIMIT #{limit}
            </script>
            """)
    List<LearningEvidence> selectRecentByConcepts(
            @Param("userId") Long userId,
            @Param("conceptIds") List<Long> conceptIds,
            @Param("limit") int limit);
}
