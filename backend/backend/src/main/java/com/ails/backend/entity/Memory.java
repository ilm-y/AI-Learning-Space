package com.ails.backend.entity;

import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import com.baomidou.mybatisplus.annotation.Version;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * Agent 长期记忆（memory）。
 * <p>
 * 与 Understanding 的区别：Understanding 是「用户对知识点的认知模型」，
 * Memory 是「Agent 未来服务这个用户的长期上下文」。
 * <p>
 * 生命周期用 status 管理，不做物理删除：
 * AI 推断出的内容默认只能停留在 CANDIDATE，用户明确表达的才可直接 ACTIVE；
 * 删除语义是 ARCHIVED，且不影响 Evidence / Understanding。
 */
@Data
@TableName("memory")
public class Memory {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    /** 可选：限定在某个学习空间内 */
    private Long learningSpaceId;

    /** 可选：限定在某个 Concept 上 */
    private Long conceptId;

    private MemoryType memoryType;

    /**
     * 作用域类型，由 {@code learningSpaceId} / {@code conceptId} 推导，不接受调用方直接指定。
     *
     * @see com.ails.backend.enums.MemoryScopeType
     */
    private MemoryScopeType scopeType;

    /**
     * 作用域的字符串身份：{@code GLOBAL} / {@code SPACE:{spaceId}} / {@code CONCEPT:{conceptId}}。
     * <p>
     * 存在的唯一理由是让 {@code UNIQUE(user_id, scope_key, memory_key)} 真的能兜底。
     * 直接对 {@code (user_id, scope_type, learning_space_id, concept_id, memory_key)} 建唯一索引
     * 是没用的 —— GLOBAL 记忆的那两列都是 NULL，而 MySQL 的唯一索引允许多行 NULL，
     * 于是同一条全局记忆可以被反复插进去。
     */
    private String scopeKey;

    /**
     * 同作用域内的记忆标识（原 {@code path}），如 {@code teaching-style}、{@code profile}。
     * <p>
     * 与 {@link #scopeKey} 一起构成「这是哪一条记忆」；{@link #content} 是它的值。
     */
    private String memoryKey;

    private String content;

    private MemoryStatus status;

    private Float importance;

    /**
     * 乐观锁版本号。
     * <p>
     * <b>要让它真的生效，{@code MybatisPlusConfig} 里必须注册
     * {@code OptimisticLockerInnerInterceptor}</b> —— 只加 {@code @Version} 是静默失效的：
     * MyBatis-Plus 不报错，UPDATE 语句里也不会带上 version 条件。
     */
    @Version
    private Integer version;

    /** 向量存储引用 */
    private String embeddingRef;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
