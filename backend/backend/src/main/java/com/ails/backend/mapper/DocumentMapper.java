package com.ails.backend.mapper;

import com.ails.backend.dto.document.ReadyDocument;
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
     *   <li>状态 READY、未软删、确实完成过索引；</li>
     *   <li>chunk_count &gt; 0，且等于<b>当前活跃版本</b>中带 vector_ref 的 chunk 数 ——
     *       重建期间的非活跃版本不参与计数；</li>
     *   <li>Embedding 身份兼容：优先用 fingerprint 精确匹配；迁移前的旧文档
     *       fingerprint 为 NULL 时退回 model 匹配。</li>
     * </ul>
     *
     * <p>返回版本号是必须的：Agent 只应检索白名单中文档的活跃版本 points。</p>
     */
    @Select("""
            <script>
            SELECT d.id AS id, d.index_version AS indexVersion
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
                    AND c.index_version = d.index_version
                    AND c.vector_ref IS NOT NULL
                    AND c.vector_ref &lt;&gt; ''
              )
            <if test="embeddingModel != null">
              AND (
                  (#{embeddingFingerprint} IS NOT NULL
                   AND d.embedding_fingerprint = #{embeddingFingerprint})
                  OR (
                      (#{embeddingFingerprint} IS NULL OR d.embedding_fingerprint IS NULL)
                      AND d.embedding_model = #{embeddingModel}
                      <if test="embeddingProvider != null">
                        AND (d.embedding_provider IS NULL
                             OR d.embedding_provider = #{embeddingProvider})
                      </if>
                      <if test="embeddingDimension != null">
                        AND (d.embedding_dimension IS NULL
                             OR d.embedding_dimension = #{embeddingDimension})
                      </if>
                  )
              )
            </if>
            ORDER BY d.id
            </script>
            """)
    List<ReadyDocument> selectReadyDocuments(
            @Param("userId") Long userId,
            @Param("spaceId") Long spaceId,
            @Param("embeddingModel") String embeddingModel,
            @Param("embeddingProvider") String embeddingProvider,
            @Param("embeddingDimension") Integer embeddingDimension,
            @Param("embeddingFingerprint") String embeddingFingerprint);

    /**
     * 找出超时仍停留在 PROCESSING 的首次索引任务。
     *
     * <p>优先使用 {@code index_started_at}；迁移前的旧行没有该值，退回
     * {@code updated_at}。阈值必须大于正常索引的最坏耗时。</p>
     */
    @Select("""
            <script>
            SELECT d.id
            FROM document d
            WHERE d.status = 'PROCESSING'
              AND d.deleted_at IS NULL
              AND (
                  (d.index_started_at IS NOT NULL AND d.index_started_at &lt; #{cutoff})
                  OR (d.index_started_at IS NULL AND d.updated_at &lt; #{cutoff})
              )
            ORDER BY d.updated_at ASC, d.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Long> selectStaleProcessingIds(
            @Param("cutoff") LocalDateTime cutoff,
            @Param("limit") int limit);

    /**
     * 超时回收 PROCESSING。自增 {@code index_attempts} 同时充当 fencing token：
     * 旧 worker 的 complete / fail 因为 attempt 不匹配而无法提交。
     */
    @Update("""
            <script>
            UPDATE document
            SET status = 'FAILED',
                index_error = #{error},
                index_started_at = NULL,
                index_attempts = index_attempts + 1
            WHERE id = #{id}
              AND status = 'PROCESSING'
              AND deleted_at IS NULL
              AND (
                  (index_started_at IS NOT NULL AND index_started_at &lt; #{cutoff})
                  OR (index_started_at IS NULL AND updated_at &lt; #{cutoff})
              )
            </script>
            """)
    int markStaleProcessingFailed(
            @Param("id") Long id,
            @Param("cutoff") LocalDateTime cutoff,
            @Param("error") String error);

    /**
     * 找出超时仍占用租约的 READY 重建。READY 重建失败不能把文档置 FAILED，
     * 否则会停掉仍在服务的旧版本。
     */
    @Select("""
            <script>
            SELECT d.id
            FROM document d
            WHERE d.status = 'READY'
              AND d.deleted_at IS NULL
              AND d.index_started_at IS NOT NULL
              AND d.index_started_at &lt; #{cutoff}
            ORDER BY d.index_started_at ASC, d.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Long> selectStaleReadyRebuildIds(
            @Param("cutoff") LocalDateTime cutoff,
            @Param("limit") int limit);

    /**
     * 放弃超时的 READY 重建：保持 READY（旧版本继续服务），只清租约并 fence 旧 worker。
     */
    @Update("""
            <script>
            UPDATE document
            SET index_error = #{error},
                index_started_at = NULL,
                index_attempts = index_attempts + 1
            WHERE id = #{id}
              AND status = 'READY'
              AND deleted_at IS NULL
              AND index_started_at IS NOT NULL
              AND index_started_at &lt; #{cutoff}
            </script>
            """)
    int markStaleReadyRebuildAbandoned(
            @Param("id") Long id,
            @Param("cutoff") LocalDateTime cutoff,
            @Param("error") String error);

    /**
     * 找出“已上传但从未提交索引”的 UPLOADING 行（进程在 insert 后崩溃）。
     */
    @Select("""
            <script>
            SELECT d.id
            FROM document d
            WHERE d.status = 'UPLOADING'
              AND d.deleted_at IS NULL
              AND d.index_started_at IS NULL
              AND d.updated_at &lt; #{cutoff}
            ORDER BY d.updated_at ASC, d.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Long> selectStaleUploadingIds(
            @Param("cutoff") LocalDateTime cutoff,
            @Param("limit") int limit);

    /**
     * UPLOADING 重试次数耗尽时收敛到 FAILED，避免无限重试。
     */
    @Update("""
            <script>
            UPDATE document
            SET status = 'FAILED',
                index_error = #{error}
            WHERE id = #{id}
              AND status = 'UPLOADING'
              AND deleted_at IS NULL
            </script>
            """)
    int markUploadingFailed(@Param("id") Long id, @Param("error") String error);

    /**
     * 待清理的 DELETING 文档。超过 {@code maxAttempts} 的行不再自动重试，
     * 但仍保持 DELETING（不参与检索），等待人工排查。
     */
    @Select("""
            <script>
            SELECT d.id
            FROM document d
            WHERE d.status = 'DELETING'
              AND d.deleted_at IS NULL
              AND d.delete_attempts &lt; #{maxAttempts}
            ORDER BY d.updated_at ASC, d.id ASC
            LIMIT #{limit}
            </script>
            """)
    List<Long> selectDeletingIds(
            @Param("maxAttempts") int maxAttempts,
            @Param("limit") int limit);

    /**
     * 按主键读取，<b>不受 {@code @TableLogic} 影响</b>：删除接口需要识别
     * 已经软删（DELETED）的文档，从而把重复删除做成幂等成功而不是 404。
     */
    @Select("SELECT * FROM document WHERE id = #{id} LIMIT 1")
    Document selectAnyById(@Param("id") Long id);

    /**
     * 删除完成：状态与软删标记同时落库。条件更新保证重复执行幂等。
     */
    @Update("""
            <script>
            UPDATE document
            SET status = 'DELETED',
                deleted_at = #{now},
                delete_error = NULL
            WHERE id = #{id}
              AND status = 'DELETING'
              AND deleted_at IS NULL
            </script>
            """)
    int markDeleted(@Param("id") Long id, @Param("now") LocalDateTime now);

    /**
     * 删除失败：保留 DELETING，记录次数与摘要，供定时任务重试。
     */
    @Update("""
            <script>
            UPDATE document
            SET delete_attempts = delete_attempts + 1,
                delete_error = #{error}
            WHERE id = #{id}
              AND status = 'DELETING'
              AND deleted_at IS NULL
            </script>
            """)
    int recordDeleteFailure(@Param("id") Long id, @Param("error") String error);

    /**
     * 仍有非活跃版本 chunk 的文档，供延迟清理任务扫描。
     */
    @Select("""
            <script>
            SELECT DISTINCT d.id
            FROM document d
            JOIN document_chunk c ON c.document_id = d.id
            WHERE d.deleted_at IS NULL
              AND d.status &lt;&gt; 'DELETING'
              AND d.index_started_at IS NULL
              AND c.index_version &lt;&gt; d.index_version
            ORDER BY d.id
            LIMIT #{limit}
            </script>
            """)
    List<Long> selectDocumentsWithStaleVersions(@Param("limit") int limit);
}
