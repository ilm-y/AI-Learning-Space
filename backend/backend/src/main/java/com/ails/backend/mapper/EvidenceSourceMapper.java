package com.ails.backend.mapper;

import com.ails.backend.entity.EvidenceSource;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

public interface EvidenceSourceMapper extends BaseMapper<EvidenceSource> {

    /**
     * 一次 IN 查询加载一批证据的来源，替代逐条 {@code loadSources} 的 N+1。
     * <p>
     * 调用方必须传非空 id 列表；返回顺序按 id 升序，服务层按 evidenceId 分组。
     */
    @Select("""
            <script>
            SELECT *
            FROM evidence_source
            WHERE evidence_id IN
            <foreach collection="evidenceIds" item="evidenceId" open="(" separator="," close=")">
                #{evidenceId}
            </foreach>
            ORDER BY id ASC
            </script>
            """)
    List<EvidenceSource> selectByEvidenceIds(@Param("evidenceIds") List<Long> evidenceIds);
}
