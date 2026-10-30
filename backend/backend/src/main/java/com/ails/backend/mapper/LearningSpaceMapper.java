package com.ails.backend.mapper;

import com.ails.backend.entity.LearningSpace;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface LearningSpaceMapper extends BaseMapper<LearningSpace> {

    /**
     * 锁住空间行，把「首次解析学习活动」串行化。
     * <p>
     * 同一空间下多条对话同时发出第一条学习消息时，若不加锁，每个事务都会看到
     * 「还没有可归属的 Session」而各自 CREATE，既产生多条重复 Session，也会在
     * learning_session 的插入/更新与 conversation.session_id 的外键检查之间形成死锁。
     * 锁粒度取空间（而不是全局），不同空间仍可并发。
     *
     * @return 空间 id；空间不存在或已软删时返回 null
     */
    @Select("SELECT id FROM learning_space WHERE id = #{id} AND deleted_at IS NULL FOR UPDATE")
    Long lockById(@Param("id") Long id);
}
