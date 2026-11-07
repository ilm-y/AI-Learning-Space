package com.ails.backend.entity;

import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 文档切片（document_chunk）—— RAG 的检索单元。
 */
@Data
@TableName("document_chunk")
public class DocumentChunk {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long documentId;

    /**
     * 所属索引构建令牌。只有与 {@code document.index_version} 相等的 chunk
     * 才是当前活跃版本；其余版本在切换成功后被延迟清理。
     */
    private Integer indexVersion;

    private Integer chunkIndex;

    private String content;

    /** PDF 页码 */
    private Integer pageNumber;

    private Integer tokenCount;

    /** 向量库引用 */
    private String vectorRef;

    /** 元数据，JSON 字符串 */
    private String metadataJson;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;
}
