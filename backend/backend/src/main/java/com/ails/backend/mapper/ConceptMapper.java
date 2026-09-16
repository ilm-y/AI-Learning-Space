package com.ails.backend.mapper;

import com.ails.backend.entity.Concept;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 知识点。除了 MyBatis-Plus 自带方法，另有两条<b>手写 SQL</b>。
 * <p>
 * 注意手写 SQL 与自带方法的区别：{@code @TableLogic} 是 MyBatis-Plus 在<b>注入自带方法时</b>
 * 拼进 SQL 的（{@code selectList} / {@code updateById} 等），手写 {@code @Select} / {@code @Update}
 * 是普通 MyBatis 语句，不会被追加 {@code deleted_at IS NULL}。本接口的两个方法正是要靠这一点。
 */
public interface ConceptMapper extends BaseMapper<Concept> {

    /**
     * 按「学习空间 + 规范名」查知识点，<b>不过滤软删</b>。
     * <p>
     * 用途是创建前的重名检查：{@code uk_concept_space_name(learning_space_id, canonical_name)}
     * 是普通唯一索引，<b>把软删的行也算在内</b>。所以只用自带方法查（会滤掉软删行）得到的
     * 「不存在」是假象 —— 真去 INSERT 就会撞唯一键。
     *
     * @return 命中行（可能是软删的），没有则 null
     */
    @Select("SELECT * FROM concept WHERE learning_space_id = #{spaceId} AND canonical_name = #{canonicalName} LIMIT 1")
    Concept selectAnyBySpaceAndName(@Param("spaceId") Long spaceId, @Param("canonicalName") String canonicalName);

    /**
     * 复活一条被软删的知识点（{@code deleted_at = NULL}）。
     * <p>
     * 必须手写：{@code updateById} 会被 @TableLogic 追加 {@code AND deleted_at IS NULL}，
     * 而软删行的该列非空，UPDATE 永远影响 0 行。
     */
    @Update("UPDATE concept SET deleted_at = NULL, updated_at = NOW() WHERE id = #{id}")
    int revive(@Param("id") Long id);
}
