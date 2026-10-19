package com.ails.backend.dto.session;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * 显式开一个新的学习活动（设计文档 §七.3「用户明确要求开始新的学习目标」）。
 * <p>
 * 这是<b>唯一</b>由用户直接创建 Session 的入口。正常情况下 Session 是懒创建的：
 * Conversation 建出来时 {@code session_id} 为 NULL，第一条有效学习消息才由
 * {@code SessionResolver} 决定归属。这里的显式创建是为了让用户能主动「另起一段」——
 * 否则「我就想从零开始学这个」只能靠换一个没被文本命中的说法来碰运气。
 */
public record SessionRequest(
        @NotNull(message = "学习空间不能为空")
        Long learningSpaceId,

        @NotBlank(message = "学习目标不能为空")
        String goal,

        /** 可空。不传就用 goal 生成一个标题。 */
        String title) {
}
