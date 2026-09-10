package com.ails.backend.entity;

import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 测试回答（test_answer）。
 * <p>
 * 测试回答本身必须成为 Evidence，而且比普通聊天观察具有更高的验证价值。
 * 但不应简单规定「所有测试证据权重永远高于所有对话证据」——
 * 实际权重由 Evidence 类型、质量、具体程度共同决定。
 */
@Data
@TableName("test_answer")
public class TestAnswer {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long taskId;

    private String userAnswer;

    private Boolean isCorrect;

    /** 判分信息，JSON 字符串，如 {"correctness":"CORRECT","feedback":"..."} */
    private String evaluation;

    /** 由 MyBatisMetaObjectHandler 填充 */
    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime answeredAt;
}
