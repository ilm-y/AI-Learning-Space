package com.ails.backend.dto.document;

/**
 * 上传结果。
 *
 * @param document  新建的文档，或同一 Space 内已存在的有效文档
 * @param duplicate 是否命中已有有效文档；true 时不会创建第二行，也不会重复写文件
 */
public record DocumentUploadResponse(DocumentResponse document, boolean duplicate) {
}
