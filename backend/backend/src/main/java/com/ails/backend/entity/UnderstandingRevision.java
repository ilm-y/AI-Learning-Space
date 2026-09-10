package com.ails.backend.entity;

import com.ails.backend.enums.UnderstandingStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 理解的历史版本（understanding_revision）。
 * <p>
 * 用来回答「用户什么时候开始理解这个概念」「为什么系统改变了判断」。
 * 一旦写入就不再修改。
 */
@Data
@TableName("understanding_revision")
public class UnderstandingRevision {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long understandingId;

    /** 同 understanding 内从 1 递增 */
    private Integer revisionNo;

    private String summary;

    /** 当时的已知部分，JSON 数组字符串 */
    private String knownParts;

    /** 当时的误区，JSON 数组字符串 */
    private String misconceptions;

    /** 当时的不确定部分，JSON 数组字符串 */
    private String uncertainParts;

    /** 当时的用户解释 */
    private String userExplanation;

    /** 当时的状态 */
    private UnderstandingStatus status;

    /** 导致本次变化的 Evidence */
    private Long triggerEvidenceId;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
