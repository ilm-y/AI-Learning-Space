package com.ails.backend.entity;

import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 知识点表（concept）。
 * <p>
 * 软删：Concept 上面挂着 Understanding / Evidence，物理删除会炸掉学习历史。
 */
@Data
@TableName("concept")
public class Concept {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 属于哪个学习空间 */
    private Long learningSpaceId;

    /** 标准名称，如 "Translation_Lookaside_Buffer" */
    private String canonicalName;

    /** 显示名称，如 "TLB" */
    private String displayName;

    /** 概念解释 */
    private String description;

    /** 难度等级 1~5 */
    private Integer difficultyLevel;

    @TableLogic(value = "null", delval = "now()")
    private LocalDateTime deletedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
