package com.ails.backend.mapper;

import com.ails.backend.entity.Document;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
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

    /**
     * 找出超时仍停在 PROCESSING 的文档。
     *
     * <p>用 {@code updated_at} 作为索引启动时间的近似值：进入 PROCESSING 的那次更新会
     * 刷新它，索引运行期间不再更新 document 行。阈值必须大于正常索引的最坏耗时，
     * 否则会误伤仍在运行的任务。</p>
     */
    @Select("""
            <script>
            SELECT d.id
            FROM document d
            WHERE d.status = 'PROCESSING'
              AND d.deleted_at IS NULL
              AND d.updated_at &lt; #{cutoff}
            ORDER BY d.updated_at ASC, d.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Long> selectStaleProcessingIds(
            @Param("cutoff") LocalDateTime cutoff,
            @Param("limit") int limit);

    /**
     * 条件更新：只有仍是 PROCESSING、未删除、且启动时间早于 cutoff 的行才转 FAILED。
     *
     * <p>这是回收的幂等性边界：重复扫描、多个实例并发扫描、或索引任务刚好完成，
     * 都只会有一次更新成功；其余更新 0 行，不会覆盖 READY 或重复写错误。</p>
     */
    @Update("""
            <script>
            UPDATE document
            SET status = 'FAILED',
                index_error = #{error}
            WHERE id = #{id}
              AND status = 'PROCESSING'
              AND deleted_at IS NULL
              AND updated_at &lt; #{cutoff}
            </script>
            """)
    int markStaleProcessingFailed(
            @Param("id") Long id,
            @Param("cutoff") LocalDateTime cutoff,
            @Param("error") String error);
}
