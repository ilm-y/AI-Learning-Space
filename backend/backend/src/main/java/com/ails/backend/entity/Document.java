package com.ails.backend.entity;

import com.ails.backend.enums.DocumentStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 文档表（document）—— RAG 的知识来源。
 * <p>
 * 数据库里还有一个 {@code active_content_hash} 生成列：
 * 未软删时等于 {@code content_hash}，软删后为 NULL，只用于
 * {@code (learning_space_id, active_content_hash)} 的 active-only 唯一约束。
 * 它不映射到实体 —— ORM 无需读写派生值，软删语义由 {@link #deletedAt} 表达。
 */
@Data
@TableName("document")
public class Document {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long learningSpaceId;

    /**
     * 冗余 owner，来源是 {@code learning_space.user_id}，不是客户端传入值。
     * <p>
     * 归属校验仍以 learning_space 为准；这一列用于检索侧做防御性过滤，
     * 以及在只拿到 document 时免去一次 join。
     */
    private Long userId;

    /**
     * 原始文件 SHA-256（64 位小写十六进制）。
     * <p>
     * 旧数据没有原始文件时保持 NULL —— 不伪造 hash。唯一索引允许任意多个 NULL，
     * 所以历史行不会阻塞 {@code (learning_space_id, content_hash)} 约束。
     * 新上传必须写入非 NULL 值，由上传服务保证。
     */
    private String contentHash;

    private String name;

    /** 原文件名 */
    private String fileName;

    /** 文件系统路径或 OSS key */
    private String storageUri;

    private String fileType;

    /** 字节数 */
    private Long size;

    private DocumentStatus status;

    private String parserVersion;

    /** 已落库的 chunk 数；索引完成后由服务端更新 */
    private Integer chunkCount;

    /** 索引尝试次数（含手工重试） */
    private Integer indexAttempts;

    /** 最近一次索引失败摘要；成功时由服务端清空 */
    private String indexError;

    /** 生成当前向量的 embedding 模型；未索引时为 NULL */
    private String embeddingModel;

    /** 生成当前向量的 provider 名（hash / openai-compatible）；未索引时为 NULL */
    private String embeddingProvider;

    /** 生成当前向量的维度；用于删除与兼容性诊断 */
    private Integer embeddingDimension;

    /**
     * Embedding 配置指纹（provider + model + dimension + endpoint 身份）。
     * <p>检索白名单按它精确过滤，避免不同模型/网关的向量被混用。
     * 迁移前的历史数据为 NULL，仅按 model 兼容。
     */
    private String embeddingFingerprint;

    /** 索引实际写入的 Qdrant collection；删除/清理直接使用，避免重算 */
    private String vectorCollection;

    /**
     * 当前活跃的索引构建令牌（不透明、单调递增）。
     * <p>与 {@code document_chunk.index_version} 对应；对外只表示“当前服务的是哪一版”。
     * 语义已从“成功次数”改为“构建令牌”，不要用它计算次数。
     */
    private Integer indexVersion;

    /** 本次构建租约开始时间；非空表示有 writer 正在构建（READY 重建时旧版本仍服务） */
    private LocalDateTime indexStartedAt;

    /** 最近一次索引成功时间 */
    private LocalDateTime indexedAt;

    /** 删除重试次数 */
    private Integer deleteAttempts;

    /** 最近一次删除失败摘要 */
    private String deleteError;

    /**
     * 软删标记：DELETING 期间仍可见（用于重试），DELETED 后置为当前时间。
     * <p>
     * 与 {@link com.ails.backend.entity.LearningSpace} 一致，使用 MyBatis-Plus
     * 的 {@code @TableLogic}，普通查询自动附加 {@code deleted_at IS NULL}。
     */
    @TableLogic(value = "null", delval = "now()")
    private LocalDateTime deletedAt;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
