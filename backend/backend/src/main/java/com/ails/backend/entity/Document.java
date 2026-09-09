package com.ails.backend.entity;

import com.ails.backend.enums.DocumentFileType;
import com.ails.backend.enums.DocumentStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 文档元数据表（document）
 */
@Data
@TableName("document")
public class Document {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long learningSpaceId;

    /** 文档显示名称 */
    private String name;

    /** 原始文件名 */
    private String fileName;

    /** 文件系统路径或 OSS key */
    private String storageUri;

    private DocumentFileType fileType;

    /** 字节数 */
    private Long size;

    private DocumentStatus status;

    /** 解析器版本，便于升级重跑 */
    private String parserVersion;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
