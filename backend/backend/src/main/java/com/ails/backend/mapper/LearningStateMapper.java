package com.ails.backend.mapper;

import com.ails.backend.entity.LearningState;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface LearningStateMapper extends BaseMapper<LearningState> {

    /**
     * 按 (user_id, concept_id) 取学习状态行的写锁，必须在事务里调用。
     * <p>
     * 投影是「查到就改、查不到就插」，且 {@code uk_state_user_concept} 是唯一索引。
     * 不锁的话，同一用户同一概念的两个并发分析会各自查到 null 再各自 INSERT，
     * 后一个撞唯一键 → 整个分析事务（含刚写好的 Evidence / Understanding）一起回滚。
     * <p>
     * 与 {@link LearningUnderstandingMapper#selectByUserAndConceptForUpdate} 同理，
     * 这一条也应当是所在事务的第一条语句（参见该方法的说明）。
     */
    @Select("SELECT * FROM learning_state WHERE user_id = #{userId} AND concept_id = #{conceptId} FOR UPDATE")
    LearningState selectByUserAndConceptForUpdate(@Param("userId") Long userId,
                                                  @Param("conceptId") Long conceptId);
}
