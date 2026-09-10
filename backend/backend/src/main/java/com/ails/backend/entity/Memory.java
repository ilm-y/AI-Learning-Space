package com.ails.backend.entity;

import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
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

    /** 层次路径，如 os/hardware-first */
    private String path;

    private String content;

    private MemoryStatus status;

    private Float importance;

    /** 向量存储引用 */
    private String embeddingRef;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
