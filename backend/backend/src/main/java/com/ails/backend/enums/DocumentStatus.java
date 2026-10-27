package com.ails.backend.enums;

/**
 * 文档处理状态（document.status）。
 * <p>
 * 枚举常量名必须与 MySQL ENUM 值逐字一致：MyBatis-Plus 按 {@link Enum#name()} 存取。
 */
public enum DocumentStatus {
    /** 已收到上传，尚未开始解析 */
    UPLOADING,
    /** 解析 / 切块 / 向量索引进行中 */
    PROCESSING,
    /** 索引完成，可以参与检索 */
    READY,
    /** 索引失败；error 与 attempts 记录在 document 表 */
    FAILED,
    /** 正在删除向量；MySQL 行仍保留，便于重试 */
    DELETING,
    /** 已删除；向量应已清理，deleted_at 已置位 */
    DELETED
}
