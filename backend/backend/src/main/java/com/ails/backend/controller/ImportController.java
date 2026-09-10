package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 导入外部对话（ChatGPT / Claude / Gemini）—— <b>骨架，尚未接入</b>。
 * <p>
 * import_task 表已在位，枚举 ImportSourceType / ImportTaskStatus 也已定义。
 */
@RestController
@RequestMapping("/api/imports")
@RequiredArgsConstructor
public class ImportController {

    private static final String NOT_READY = "导入能力尚未接入";

    @PostMapping
    public void create() {
        throw new BusinessException(501, NOT_READY);
    }

    @GetMapping("/{id}")
    public void get(@PathVariable Long id) {
        throw new BusinessException(501, NOT_READY);
    }
}
