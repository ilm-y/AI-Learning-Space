package com.ails.backend.entity;

import com.ails.backend.enums.QuestionType;
import com.ails.backend.enums.TestTaskStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 主动验证任务（test_task）。
 * <p>
 * 测试嵌入当前对话，而不是单独建一个考试页面，因此挂在 conversation 上。
 * 是否出题由 Evidence Gap + Verification Policy 决定，不是「confidence 低就出题」。
 */
@Data
@TableName("test_task")
public class TestTask {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    /** 针对哪个知识点 */
    private Long conceptId;

    /** 测试嵌入的对话 */
    private Long conversationId;

    private QuestionType questionType;

    /** 题目内容 */
    private String questionText;

    /** 选择题选项，JSON 数组字符串 */
    private String options;

    /** 标准答案 */
    private String correctAnswer;

    /** 简答题评价标准，JSON 字符串 */
    private String evaluationRubric;

    private String hint;

    private TestTaskStatus status;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    private LocalDateTime expiredAt;
}
