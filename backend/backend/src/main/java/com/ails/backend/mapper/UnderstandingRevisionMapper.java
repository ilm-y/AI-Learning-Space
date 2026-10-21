package com.ails.backend.mapper;

import com.ails.backend.entity.UnderstandingRevision;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface UnderstandingRevisionMapper extends BaseMapper<UnderstandingRevision> {

    /**
     * 取某个快照当前的最大版本号，<b>加锁读</b>。
     * <p>
     * 为什么不能用普通的 {@code COUNT(*)} / {@code MAX(revision_no)}：
     * 写入方虽然都先拿 {@code learning_understanding} 的行锁，但 InnoDB 的一致性读视图
     * 是在事务里第一次<b>普通</b> SELECT 时建立的 —— 分析事务在调用 apply 之前往往已经
     * 读过证据、概念，视图早于对方提交。此时普通 COUNT 看到的是旧值，两个事务就会算出
     * 同一个 revision_no，撞 {@code uk_revision_no}。
     * <p>
     * 加锁读是「当前读」，永远看最新已提交版本，并顺带在 {@code (understanding_id, revision_no)}
     * 索引范围上取 next-key 锁。同理解快照的写入本来就被行锁串行化，这里不会引入新的等待顺序。
     */
    @Select("SELECT COALESCE(MAX(revision_no), 0) FROM understanding_revision "
            + "WHERE understanding_id = #{understandingId} FOR UPDATE")
    Integer selectMaxRevisionNoForUpdate(@Param("understandingId") Long understandingId);
}
