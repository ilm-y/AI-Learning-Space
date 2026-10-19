package com.ails.backend.enums;

/**
 * Memory 的作用域，见 {@code docs/database&API_design.md} §十八~二十一。
 * <p>
 * 这个字段存在的理由很具体：{@code learning_space_id IS NULL} 在 SQL 里既可能表示
 * 「全局记忆」也可能表示「没填」，而检索方（Python Agent）每次都会带上当前 spaceId，
 * 于是全局记忆永远匹配不上、静默消失。把作用域显式写出来之后，
 * 「GLOBAL 对任意 Space 可见」变成一条可以直接写进 WHERE 的规则，不再依赖 NULL 的语义。
 */
public enum MemoryScopeType {

    /** 与具体学习空间无关：用户的通用偏好、稳定的学习方式。任意 Space 都可检索。 */
    GLOBAL,

    /** 只在某一个学习空间内可见。 */
    SPACE,

    /** 只在某一个知识点上下文里可见（同时还要落在那条记忆所属的 Space 内）。 */
    CONCEPT
}
