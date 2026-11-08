package com.ails.backend.mapper;

import com.ails.backend.dto.document.VersionCollection;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * {@code document_index_version}：每个索引版本实际写入的 Qdrant collection。
 *
 * <p><b>为什么需要这张表：</b>{@code document.vector_collection} 只保存“当前活跃版本”；
 * 一旦切换活跃版本，旧版本 points 的 collection 就失去定位信息，清理只能打到新
 * collection（RAG-02）。按 {@code (document_id, index_version)} 记录后，清理可以
 * 精确回到旧版本自己的 collection。</p>
 *
 * <p>没有记录的行表示“位置不可定位”：迁移前已存在的非活跃版本、以及
 * {@code vector_collection} 为 NULL 的 legacy 文档。清理不会猜测 collection 名，
 * 只会记录残留边界。</p>
 */
public interface DocumentIndexVersionMapper {

    @Insert("""
            INSERT INTO document_index_version
                (document_id, index_version, vector_collection)
            VALUES (#{documentId}, #{indexVersion}, #{vectorCollection})
            ON DUPLICATE KEY UPDATE
                vector_collection = VALUES(vector_collection)
            """)
    int upsert(@Param("documentId") Long documentId,
               @Param("indexVersion") Integer indexVersion,
               @Param("vectorCollection") String vectorCollection);

    @Select("""
            SELECT vector_collection
            FROM document_index_version
            WHERE document_id = #{documentId}
              AND index_version = #{indexVersion}
            """)
    String selectCollection(@Param("documentId") Long documentId,
                            @Param("indexVersion") Integer indexVersion);

    @Select("""
            <script>
            SELECT index_version AS indexVersion,
                   vector_collection AS vectorCollection
            FROM document_index_version
            WHERE document_id = #{documentId}
              AND index_version IN
              <foreach collection="versions" item="version" open="(" separator="," close=")">
                #{version}
              </foreach>
            </script>
            """)
    List<VersionCollection> selectByVersions(@Param("documentId") Long documentId,
                                             @Param("versions") List<Integer> versions);

    @Delete("""
            <script>
            DELETE FROM document_index_version
            WHERE document_id = #{documentId}
              AND index_version IN
              <foreach collection="versions" item="version" open="(" separator="," close=")">
                #{version}
              </foreach>
            </script>
            """)
    int deleteVersions(@Param("documentId") Long documentId,
                       @Param("versions") List<Integer> versions);

    @Delete("DELETE FROM document_index_version WHERE document_id = #{documentId}")
    int deleteByDocument(@Param("documentId") Long documentId);
}
