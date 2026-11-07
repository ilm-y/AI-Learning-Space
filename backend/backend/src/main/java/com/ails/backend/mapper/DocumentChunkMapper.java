package com.ails.backend.mapper;

import com.ails.backend.entity.DocumentChunk;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

public interface DocumentChunkMapper extends BaseMapper<DocumentChunk> {

    /**
     * 删除某个构建版本的全部 chunk。新构建开始前用它清掉同一 attempt 的残留
     * （正常不会存在，除非进程在写入后崩溃）。
     */
    @Delete("""
            DELETE FROM document_chunk
            WHERE document_id = #{documentId}
              AND index_version = #{indexVersion}
            """)
    int deleteByVersion(
            @Param("documentId") Long documentId,
            @Param("indexVersion") Integer indexVersion);

    /**
     * 删除除活跃版本外的全部 chunk。检索只认活跃版本，因此旧版本可以延迟清理；
     * 清理失败不影响正确性。
     */
    @Delete("""
            <script>
            DELETE FROM document_chunk
            WHERE document_id = #{documentId}
              AND index_version <![CDATA[ <> ]]> #{activeVersion}
            </script>
            """)
    int deleteOtherVersions(
            @Param("documentId") Long documentId,
            @Param("activeVersion") Integer activeVersion);

    /**
     * 删除文档的全部 chunk（删除流程使用）。
     */
    @Delete("DELETE FROM document_chunk WHERE document_id = #{documentId}")
    int deleteAll(@Param("documentId") Long documentId);

    /**
     * 某文档当前存在的非活跃版本号，供 Qdrant 版本清理使用。
     */
    @Select("""
            <script>
            SELECT DISTINCT index_version
            FROM document_chunk
            WHERE document_id = #{documentId}
              AND index_version <![CDATA[ <> ]]> #{activeVersion}
            </script>
            """)
    List<Integer> selectOtherVersions(
            @Param("documentId") Long documentId,
            @Param("activeVersion") Integer activeVersion);
}
