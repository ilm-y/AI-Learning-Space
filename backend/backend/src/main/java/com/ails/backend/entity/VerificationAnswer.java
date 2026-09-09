package com.ails.backend.entity;

import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 验证回答表（verification_answer），一个 task 只允许一次有效回答
 */
@Data
@TableName("verification_answer")
public class VerificationAnswer {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** verification_task.id */
    private Long taskId;

    /** 用户回答内容 */
    private String userAnswer;

    /** 系统自动比对结果 */
    private Boolean isCorrect;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime answeredAt;
}
