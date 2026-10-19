package com.ails.backend.entity;

import com.ails.backend.enums.SessionStatus;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 一次学习活动的容器（learning_session）。
 * <p>
 * 注意：Session 不承担长期记忆。它更接近 Thread / Activity，
 * 而不是长期 Memory —— 有价值的长期信息应被提取到 Understanding / Memory。
 */
@Data
@TableName("learning_session")
public class LearningSession {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long learningSpaceId;

    private String title;

    /** 当前关注点 */
    /** 这一阶段最终要解决什么（长期）。与 {@link #currentFocus} 的区别见 docs/session-design.md §一.1 */
    private String goal;

    /** 当前卡在哪里 / 正在解决什么（短期）。主要给 Agent 用。 */
    private String currentFocus;

    private SessionStatus status;

    private LocalDateTime startedAt;

    private LocalDateTime lastActiveAt;

    private LocalDateTime completedAt;
}
