package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 文档上传与解析 —— <b>骨架，尚未接入</b>。
 * <p>
 * 表结构（document / document_chunk）已经在位，接口先占住路径，避免前端联调时到处改路由。
 * 真正实现时要做的是：上传 → 解析切块 → 向量化 → 写入 document_chunk.vector_ref。
 * 现在直接返回 501，而不是返回一个空的成功 —— 假装成功比明确说没做更浪费时间。
 */
@RestController
@RequestMapping("/api/documents")
@RequiredArgsConstructor
public class DocumentController {

    private static final String NOT_READY = "文档能力尚未接入";

    @PostMapping
    public void upload() {
        throw new BusinessException(501, NOT_READY);
    }

    @GetMapping
    public void list() {
        throw new BusinessException(501, NOT_READY);
    }

    @GetMapping("/{id}")
    public void get(@PathVariable Long id) {
        throw new BusinessException(501, NOT_READY);
    }
}
