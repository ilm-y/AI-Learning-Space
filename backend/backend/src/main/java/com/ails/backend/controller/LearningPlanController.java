package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 学习计划 —— <b>骨架，尚未接入</b>。
 */
@RestController
@RequestMapping("/api/learning-plans")
@RequiredArgsConstructor
public class LearningPlanController {

    private static final String NOT_READY = "学习计划能力尚未接入";

    @GetMapping
    public void list() {
        throw new BusinessException(501, NOT_READY);
    }

    @PostMapping
    public void create() {
        throw new BusinessException(501, NOT_READY);
    }

    @PatchMapping("/{id}")
    public void update(@PathVariable Long id) {
        throw new BusinessException(501, NOT_READY);
    }
}
