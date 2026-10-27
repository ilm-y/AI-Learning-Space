package com.ails.backend.service;

/**
 * 文档索引链路中的可分类失败。消息会写入 {@code document.index_error}，
 * 调用方绝不能把它当成成功。
 */
public class DocumentIndexException extends RuntimeException {

    private final int code;

    public DocumentIndexException(int code, String message) {
        super(message);
        this.code = code;
    }

    public DocumentIndexException(int code, String message, Throwable cause) {
        super(message, cause);
        this.code = code;
    }

    public int getCode() {
        return code;
    }
}
