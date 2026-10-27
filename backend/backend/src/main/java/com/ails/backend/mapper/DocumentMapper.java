package com.ails.backend.mapper;

import com.ails.backend.entity.Document;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

public interface DocumentMapper extends BaseMapper<Document> {

    /**
     * 知识检索可用的文档白名单。
     *
     * <p>只返回同时满足以下条件的文档：</p>
     * <ul>
     *   <li>属于当前用户且属于指定 Space（Space 归属仍由 Service 先校验）；</li>
     *   <li>状态 READY、未软删、确实完成过索引（indexed_at / embedding_model 非空）；</li>
     *   <li>chunk_count &gt; 0，且等于实际带 vector_ref 的 chunk 数 —— 防止
     *       「状态是 READY，但 chunk 写回不完整 / Qdrant 未全部索引」的历史脏数据。</li>
     * </ul>
     *
     * <p>只比较 embedding_model 的完整字符串，不做前缀匹配：Hash 与真实模型
     * 的 collection 不同，不能用当前模型的查询向量去检索旧模型的文档。</p>
     */
    @Select("""
            <script>
            SELECT d.id
            FROM document d
            WHERE d.learning_space_id = #{spaceId}
              AND d.user_id = #{userId}
              AND d.status = 'READY'
              AND d.deleted_at IS NULL
              AND d.indexed_at IS NOT NULL
              AND d.embedding_model IS NOT NULL
              AND d.embedding_model &lt;&gt; ''
              AND d.chunk_count &gt; 0
              AND d.chunk_count = (
                  SELECT COUNT(*)
                  FROM document_chunk c
                  WHERE c.document_id = d.id
                    AND c.vector_ref IS NOT NULL
                    AND c.vector_ref &lt;&gt; ''
              )
            <if test="embeddingModel != null">
              AND d.embedding_model = #{embeddingModel}
            </if>
            ORDER BY d.id
            </script>
            """)
    List<Long> selectReadyIndexedIds(
            @Param("userId") Long userId,
            @Param("spaceId") Long spaceId,
            @Param("embeddingModel") String embeddingModel);
}
