package com.ails.backend.entity;

import com.ails.backend.enums.DocumentStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 文档表（document）—— RAG 的知识来源。
 */
@Data
@TableName("document")
public class Document {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long learningSpaceId;

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

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
