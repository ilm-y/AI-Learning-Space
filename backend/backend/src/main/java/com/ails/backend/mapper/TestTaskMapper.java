package com.ails.backend.mapper;

import com.ails.backend.entity.TestTask;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface TestTaskMapper extends BaseMapper<TestTask> {

    /**
     * 取任务行的写锁（{@code SELECT ... FOR UPDATE}），必须在事务里调用。
     * <p>
     * 提交答案时要先锁住这一行：<b>「读状态判断 PENDING」与「写入 ANSWERED」之间不能有窗口</b>。
     * 不锁的话，用户双击提交会让两个请求同时读到 PENDING、同时插入 test_answer，
     * 后一个撞 {@code uk_answer_task} 变成 500，而接口契约写的是 409。
     */
    @Select("SELECT * FROM test_task WHERE id = #{id} FOR UPDATE")
    TestTask selectByIdForUpdate(@Param("id") Long id);
}
