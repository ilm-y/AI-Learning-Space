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
 * 当前理解快照（learning_understanding）。
 * <p>
 * 一个 user × concept 只有一行。这里是「理解」的 source of truth：
 * {@link LearningState#getStatus()} 只是它的 Materialized Projection。
 * <p>
 * 它是快照，不承担历史 —— 历史由 {@link UnderstandingRevision} 保存。
 * 每次真正发生变化都必须留下一条 Revision，并通过 currentRevisionId 指向最新那条。
 * <p>
 * 不保存 mastery / confidence / stability 之类的数值（第一版口径）。
 */
@Data
@TableName("learning_understanding")
public class LearningUnderstanding {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long conceptId;

    /** 当前理解总结 */
    private String summary;

    /** 已理解部分，JSON 数组字符串 */
    private String knownParts;

    /** 当前/历史误区，JSON 数组字符串 */
    private String misconceptions;

    /** 不确定部分，JSON 数组字符串 */
    private String uncertainParts;

    /** 最近一次用户自己的解释 */
    private String latestUserExplanation;

    private UnderstandingStatus status;

    /** 当前 Revision（不加外键，避免与 understanding_revision 循环依赖） */
    private Long currentRevisionId;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
