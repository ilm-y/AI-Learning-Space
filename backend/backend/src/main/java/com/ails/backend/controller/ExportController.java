package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 学习内容导出 —— <b>骨架，尚未接入</b>。
 */
@RestController
@RequestMapping("/api/export")
@RequiredArgsConstructor
public class ExportController {

    @PostMapping
    public void export() {
        throw new BusinessException(501, "导出能力尚未接入");
    }
}
