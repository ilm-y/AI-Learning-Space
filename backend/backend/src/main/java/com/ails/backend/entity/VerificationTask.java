package com.ails.backend.entity;

import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.VerificationTaskStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 验证任务表（verification_task，AI 主动提问）
 */
@Data
@TableName("verification_task")
public class VerificationTask {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    /** 针对哪个知识点 */
    private Long conceptId;

    /** 可空 */
    private Long learningSessionId;

    private QuestionType questionType;

    /** 题目内容 */
    private String questionText;

    /** 选择题选项，JSON 数组，如 ["A. 会","B. 不会"] */
    private String options;

    /** 标准答案 */
    private String correctAnswer;

    /** 提示（选填） */
    private String hint;

    private VerificationTaskStatus status;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    /** 过期时间，如 24 小时后失效 */
    private LocalDateTime expiredAt;
}
