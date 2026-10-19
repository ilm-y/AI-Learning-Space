package com.ails.backend.config;

import com.baomidou.mybatisplus.annotation.DbType;
import com.baomidou.mybatisplus.extension.plugins.MybatisPlusInterceptor;
import com.baomidou.mybatisplus.extension.plugins.inner.OptimisticLockerInnerInterceptor;
import com.baomidou.mybatisplus.extension.plugins.inner.PaginationInnerInterceptor;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * MyBatis-Plus 配置：Mapper 扫描 + 分页插件 + 乐观锁插件。
 * <p>
 * <b>乐观锁插件不是可选项</b>：{@code @Version} 只是标记，真正把 version 加进 UPDATE 的
 * WHERE 条件并自增的是 {@link OptimisticLockerInnerInterceptor}。不注册它，带 {@code @Version}
 * 的实体照常读写，只是并发更新会静默互相覆盖 —— 没有任何报错提示。
 * <p>
 * 目前只有 {@code Memory} 用了 {@code @Version}（见 {@code Memory#version} 的说明）。
 */
@Configuration
@MapperScan("com.ails.backend.mapper")
public class MybatisPlusConfig {

    /**
     * 注册顺序有讲究：MyBatis-Plus 按加入顺序编排内部拦截器，
     * 分页插件官方建议放在最后（它要改写 SQL 的尾部）。
     */
    @Bean
    public MybatisPlusInterceptor mybatisPlusInterceptor() {
        MybatisPlusInterceptor interceptor = new MybatisPlusInterceptor();
        interceptor.addInnerInterceptor(new OptimisticLockerInnerInterceptor());
        interceptor.addInnerInterceptor(new PaginationInnerInterceptor(DbType.MYSQL));
        return interceptor;
    }
}
